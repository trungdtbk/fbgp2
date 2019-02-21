fBGP
===

Introduction
------------
fBGP (a flow-based BGP) is an implementation of a BGP router on top of Exabgp.
fBGP runs BGP and push routes to Faucet SDN controller who in turn translates routes
into FIB rules and installs into OpenFlow switches.
fBGP exposes API to allow "mapping" between a peer and any route it has learned.
