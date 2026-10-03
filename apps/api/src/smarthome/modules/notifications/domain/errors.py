class NotificationsError(Exception):
    pass


class AlertNotFound(NotificationsError):
    pass


class NotificationNotFound(NotificationsError):
    pass


class InvalidWebhook(NotificationsError):
    pass


class InvalidPreferences(NotificationsError):
    pass


class NoWebhook(NotificationsError):
    pass
