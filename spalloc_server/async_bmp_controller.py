""" Provide (basic) asynchronous control over a BMP responsible for controlling
a whole rack.
"""

import threading
import logging
from collections import namedtuple, deque

from spinnman.transceiver import create_transceiver_from_hostname
from spinnman.model import BMPConnectionData
from spinnman.constants import SCP_SCAMP_PORT

from .links import Links
from six import reraise
import sys
import time

# The first BMP version with FPGA register support
_BMP_VER_MIN = 2

_N_FPGA_RETRIES = 3

_N_POWER_LINK_RETRIES = 1

_SECONDS_BETWEEN_RETRIES = 15


class AsyncBMPController(object):
    """ An object which provides an asynchronous interface to a power and link
    control commands of a SpiNNaker BMP.

    Since BMP commands, particularly power-on commands, take some time to
    complete, it is desirable for them to be executed asynchronously. This
    object uses a SpiNNMan :py:class:`~spinnman.transceiver.Transceiver` object
    to communicate with a BMP controlling a single frame of boards.

    Power and link configuration commands are queued and executed in a
    background thread. When a command completes, a user-supplied callback is
    called.

    Sequential power commands of the same type (on/off) are coalesced into a
    single power on command. When a power command is sent, all previous link
    configuration commands queued for that board are skipped. Additionally, all
    power commands are completed before link configuration commands are carried
    out.
    """

    def __init__(self, hostname, on_thread_start=None):
        """ Start a new asynchronous BMP Controller

        Parameters
        ----------
        hostname : str
            The hostname/IP of the BMP to connect to.
        on_thread_start : function() or None
            *Optional.* A function to be called by the controller's background
            thread before it starts. This can be used to ensure proper
            sequencing/handing-over between two AsyncBMPControllers connected
            to the same machine.
        """
        self._on_thread_start = on_thread_start

        self._transceiver = create_transceiver_from_hostname(
            None, 5, bmp_connection_data=[
                BMPConnectionData(0, 0, hostname, [0], SCP_SCAMP_PORT)])
        self._hostname = hostname

        self._stop = False

        # A lock which must be held when modifying the state of this object
        self._lock = threading.RLock()

        # An event fired whenever some new interaction with the BMP is
        # required.
        self._requests_pending = threading.Event()

        # A queue of power change states
        self._power_requests = deque()

        # A queue of link-enabled state changes
        self._link_requests = deque()

        self._thread = threading.Thread(
            target=self._run,
            name="<BMP control thread for {}>".format(hostname))
        self._thread.start()

    def __enter__(self):
        """ When used as a context manager, make requests 'atomic'.
        """
        self._lock.acquire()

    def __exit__(self, _type=None, _value=None, _traceback=None):
        self._lock.release()
        return False

    def set_power(self, board, state, on_done):
        """ Set the power state of a single board.

        Parameters
        ----------
        board : int
            The board to control.
        state : bool
            True = on, False = off.
        on_done : function(success)
            Function to call when the command completes. May be called from
            another thread. Success is a bool which is True if the command
            completed successfully and False if it did not (or was cancelled).
        """
        # Verify that our arguments are sane
        board = int(board)
        state = bool(state)
        with self._lock:
            assert not self._stop

            # Enqueue the request
            self._power_requests.append(_PowerRequest(state, board, on_done))
            self._requests_pending.set()

            # Cancel any existing link enable commands for this board
            cancelled = []
            for request in list(self._link_requests):
                if request.board == board:
                    self._link_requests.remove(request)
                    cancelled.append(request)

        for request in cancelled:
            request.on_done(False, "Cancelled")

    def set_link_enable(self, board, link, enable, on_done):
        """ Enable or disable a link.

        Parameters
        ----------
        board : int
            The board on which the link resides.
        link : :py:class:`spalloc_server.links.Links`
            The link to configure.
        enable : bool
            True = link enabled, False = link disabled.
        on_done : function(success)
            Function to call when the command completes. May be called from
            another thread. Success is a bool which is True if the command
            completed successfully and False if it did not (or was cancelled).
        """
        # Verify that our arguments are sane
        board = int(board)
        enable = bool(enable)
        with self._lock:
            assert not self._stop

            # Enqueue the request
            self._link_requests.append(
                _LinkRequest(board, link, enable, on_done))
            self._requests_pending.set()

    def stop(self):
        """ Stop the background thread, as soon as possible after completing\
            all queued actions.
        """
        with self._lock:
            self._stop = True
            self._requests_pending.set()

    def join(self):
        """ Wait for the thread to actually stop.
        """
        self._thread.join()

    def _good_fpga(self, board, fpga):
        fpga_id = self._transceiver.read_fpga_register(
            fpga_num=fpga, register=_FPGA_FLAG_REGISTER_ADDRESS,
            board=board, cabinet=0, frame=0)
        ok = (fpga_id & _FPGA_FLAG_ID_MASK) == fpga
        if not ok:  # pragma: no cover
            logging.warn(
                "FPGA %d on board %d of %s has incorrect FPGA ID flag %d",
                fpga, board, self._hostname, fpga_id & _FPGA_FLAG_ID_MASK)
        return ok

    def _power_board(self, boards, power):
        n_retries = _N_POWER_LINK_RETRIES
        while n_retries >= 0:
            try:
                if power:
                    self._transceiver.power_on(
                        boards=boards, frame=0, cabinet=0)
                else:
                    self._transceiver.power_off(
                        boards=boards, frame=0, cabinet=0)
                return
            except Exception:
                if n_retries == 0:
                    reraise(*sys.exc_info())
                logging.exception("Retry power command")
                n_retries -= 1
                time.sleep(_SECONDS_BETWEEN_RETRIES)

    def _boot_board(self, boards):
        # FPGAs are checked after power on - assume incorrect to start
        boards_to_power = boards
        for _try in range(_N_FPGA_RETRIES):
            # Power on - note don't need to power off if in subsequent
            # run of the loop as the BMP handles this correctly
            self._power_board(boards_to_power, power=True)

            # Check if the FPGA number is correct on each FPGA
            retry_boards = []
            for board in boards_to_power:
                # skip board if old BMP version
                vi = self._transceiver.read_bmp_version(
                    board=board, frame=0, cabinet=0)
                if vi.version_number[0] < _BMP_VER_MIN:
                    continue

                # check each FPGA on board
                if not all(self._good_fpga(board, fpga)
                           for fpga in range(_N_FPGAS)):
                    retry_boards.append(board)

            # try again with incorrect boards only
            if len(retry_boards):
                boards_to_power = retry_boards
            else:
                return
        else:  # pragma: no cover
            raise Exception(
                "Could not get correct FPGA ID after {} tries".format(
                    _N_FPGA_RETRIES))

    def _set_board_state(self, state, board):
        """ Set the power state of a board.

        :param state: What to set the state to. True for on, False for off
        :type state: bool
        :param board: Which board or boards to set the state of
        :type board: int or iterable
        """
        try:
            # If powering on...
            if state:
                self._boot_board(board)
            # If powering off...
            else:
                self._power_board(board, power=False)
            return True, None
        except Exception:
            reason = \
                "Failed to set board power on BMP {}, boards {}, state={}."\
                .format(self._hostname, board, state)

            # Communication issue with the machine, log it
            logging.exception(reason)
            return False, reason

    def _set_link_state(self, link, enable, board):
        """ Set the power state of a link.

        :param link: The link (direction) to set the enable-state of.
        :type link: value in Links enum
        :param state: What to set the state to. True for on, False for off.
        :type state: bool
        :param board: Which board or boards to set the link enable-state of.
        :type board: int or iterable
        """
        try:
            # skip FPGA link configuration if old BMP version
            vi = self._transceiver.read_bmp_version(
                board=board, frame=0, cabinet=0)
            if vi.version_number[0] < _BMP_VER_MIN:
                return True, None

            fpga, addr = FPGA_LINK_STOP_REGISTERS[link]
            n_retries = _N_POWER_LINK_RETRIES
            while n_retries >= 0:
                try:
                    self._transceiver.write_fpga_register(
                        fpga, addr, int(not enable), board=board, frame=0,
                        cabinet=0)
                except Exception:
                    if n_retries == 0:
                        reraise(*sys.exc_info())
                    logging.exception("Retry link command")
                    n_retries -= 1
                    time.sleep(_SECONDS_BETWEEN_RETRIES)
            return True, None
        except Exception:
            reason = "Failed to set link state on BMP {}, board {}, link {},"\
                " enable={}.".format(self._hostname, board, link, enable)

            # Communication issue with the machine, log it
            logging.exception(reason)
            return False, reason

    def _run(self):
        """ The background thread for interacting with the BMP.
        """
        try:
            if self._on_thread_start is not None:
                self._on_thread_start()

            while True:
                self._requests_pending.wait()

                # Priority 0: Power commands
                power_request = self._get_atomic_power_request()
                if power_request:
                    # Send the power command
                    success, reason = self._set_board_state(
                        power_request.state, power_request.board)

                    # Alert all waiting threads
                    for on_done in power_request.on_done:
                        on_done(success, reason)

                    continue

                # Priority 1: Link enable/disable commands
                link_request = self._get_atomic_link_request()
                if link_request:
                    # Set the link state, as required
                    success, reason = self._set_link_state(
                        link_request.link, link_request.enable,
                        link_request.board)

                    # Alert waiting thread
                    link_request.on_done(success, reason)

                    continue

                # If nothing left in the queues, clear the request flag and
                # break out of queue-processing loop.
                with self._lock:
                    if (not self._power_requests and  # pragma: no branch
                            not self._link_requests):
                        self._requests_pending.clear()

                        # If we've been told to stop, actually stop the thread
                        # now
                        if self._stop:  # pragma: no branch
                            return
        except Exception:  # pragma: no cover
            # If the thread crashes something has gone wrong with this program
            # (not the machine), setting _stop will cause set_power and
            # set_link_enable to fail, hopefully propagating news of this
            # crash..
            with self._lock:
                self._stop = True
            raise

    def _get_atomic_power_request(self):
        """ If any power requests are outstanding, return a (boards, state)\
            tuple which combines as many of the requests at the head of the\
            queue as possible.

        :rtype: :py:class:`._PowerRequest` or None
        """
        with self._lock:
            # Special case: no requests
            if not self._power_requests:
                return None

            # Otherwise, accumulate as many boards as possible
            state = self._power_requests[0].state
            boards = list()
            on_done = []
            while (self._power_requests and
                   self._power_requests[0].state == state):
                request = self._power_requests.popleft()
                boards.append(request.board)
                on_done.append(request.on_done)
            return _PowerRequest(state, boards, on_done)

    def _get_atomic_link_request(self):
        """ Pop the next link state change request, if one exists.

        :rtype: :py:class:`._LinkRequest` or None
        """
        with self._lock:
            if not self._link_requests:
                return None
            return self._link_requests.popleft()


class _PowerRequest(namedtuple("_PowerRequest", "state board on_done")):
    """ Requests that a specific board should have its power state set to a
    particular value.

    Parameters
    ----------
    state : bool
        On (True) or off (False).
    board : int
        Board to change the state of
    on_done : function(success)
        A function to call when the request has been completed.
    """

    # Python 3.4 Workaround: https://bugs.python.org/issue24931
    __slots__ = tuple()


class _LinkRequest(namedtuple("_LinkRequest", "board link enable on_done")):
    """ Requests that a specific board should have its power state set to a
    particular value.

    Parameters
    ----------
    board : int
        Board whose link should be blocked/unblocked
    link : :py:class:`spalloc_server.links.Link`
        The link whose state should be changed
    enable : bool
        State of the link: Enabled (True), disabled (False).
    on_done : function(success)
        A function to call when the request has been completed.
    """

    # Python 3.4 Workaround: https://bugs.python.org/issue24931
    __slots__ = tuple()


# The number of FPGAs
_N_FPGAS = 3

# The FLAG register address in the FPGAs
_FPGA_FLAG_REGISTER_ADDRESS = 0x40004

# The FPGA ID field within the FLAG register value
_FPGA_FLAG_ID_MASK = 0x3

# Gives the FPGA number and register addresses for the STOP register (which
# disables outgoing traffic on a high-speed link) for each link direction.
# https://github.com/SpiNNakerManchester/spio/tree/master/designs/spinnaker_fpgas#spi-interface
_REG_STOP_OFFSET = 0x5C
FPGA_LINK_STOP_REGISTERS = {
    Links.east: (0, 0x00000000 + _REG_STOP_OFFSET),
    Links.south: (0, 0x00010000 + _REG_STOP_OFFSET),
    Links.south_west: (1, 0x00000000 + _REG_STOP_OFFSET),
    Links.west: (1, 0x00010000 + _REG_STOP_OFFSET),
    Links.north: (2, 0x00000000 + _REG_STOP_OFFSET),
    Links.north_east: (2, 0x00010000 + _REG_STOP_OFFSET),
}
