"""anysda-vpn Telegram bot: one process for the admin chat and for VPN clients.

Admin chat (TELEGRAM_CHAT_ID): /status, /nodes, /clients with client management
on inline buttons, alerts of the background monitor, notifications of the panel.
Any other private chat is a client: /start <password> links the chat by an
invitation, then a menu of devices and their configs.

The panel talks to the bot over POST /event on 127.0.0.1:TGBOT_EVENT_PORT.
Telegram is reached only through TG_PROXY; settings are described in config.py.
"""
