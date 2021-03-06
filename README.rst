SpiNNaker Machine Partitioning and Allocation Server (``spalloc_server``)
=========================================================================

.. image:: https://img.shields.io/pypi/v/spalloc_server.svg?style=flat
 :target: https://pypi.python.org/pypi/spalloc_server/
 :alt: PyPi version
.. image:: https://readthedocs.org/projects/spalloc_server/badge/?version=stable
 :target: http://spalloc_server.readthedocs.org/
 :alt: Documentation
.. image:: https://travis-ci.org/SpiNNakerManchester/spalloc_server.svg?branch=master
 :target: https://travis-ci.org/SpiNNakerManchester/spalloc_server
 :alt: Build Status
.. image:: https://coveralls.io/repos/SpiNNakerManchester/spalloc_server/badge.svg?branch=master
 :target: https://coveralls.io/r/SpiNNakerManchester/spalloc_server?branch=master
 :alt: Coverage Status

A SpiNNaker machine management application which manages the partitioning and
allocation of large SpiNNaker machines into smaller fragments for many
simultaneous users.

This package just contains a server which exposes a simple TCP interface to
clients to enable them to request hardware.

The `documentation <http://spalloc-server.readthedocs.org/>`_ describes the
process of configuring and running servers, the simple JSON-based client
protocol and an overview of the server's implementation.
