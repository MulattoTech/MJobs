"""Explicit notification provider registry; disabled configuration creates no client."""

from services.notifications.contract import NotificationError


def create_provider(config, *, transport=None, environ=None):
    if not config.enabled:
        return None
    if config.provider == "ntfy":
        from services.notifications.providers.ntfy import NtfyProvider
        return NtfyProvider(config, transport=transport, environ=environ)
    if config.provider == "discord":
        from services.notifications.providers.discord import DiscordProvider
        return DiscordProvider(config, transport=transport, environ=environ)
    raise NotificationError("NOTIFICATION_PROVIDER_UNSUPPORTED")


def capability_descriptors():
    from dataclasses import asdict
    from services.notifications.providers.discord import DiscordProvider
    from services.notifications.providers.ntfy import NtfyProvider
    # Pure descriptor: no client, credential lookup, socket, or delivery starts.
    return (asdict(NtfyProvider.capabilities()), asdict(DiscordProvider.capabilities()))
