"""Cotton Seed Oil Cake Marketplace - "Jan Purna" (जन पूर्णा).

Single-vendor ordering marketplace delivered as a Telegram bot (v1).

This package is organized as a *modular monolith*: each subsystem named in the
design (Bot_Interface, Auth, Catalog, Cart, Order, Payment, Fulfillability,
Notification, Admin) is a Python package with a real interface boundary. Domain
packages never import the Telegram SDK; all channel-specific code lives behind
the ``bot_interface`` seam so additional channels (WhatsApp / web) stay cheap.
"""

__version__ = "0.1.0"
