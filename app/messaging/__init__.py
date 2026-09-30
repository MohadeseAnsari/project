"""Outbound delivery to messaging channels (Bale, Telegram).

The package is split by responsibility so the sender Worker stays readable:

- ``formatting``   turns a generated article into the exact message text.
- ``platforms``    one thin HTTP client per messaging platform.
- ``dispatcher``   the queue logic: which (article, channel) pairs still need
                   a send, claiming one, and recording the outcome.
- ``worker``       the polling loop and console entry point.
"""
