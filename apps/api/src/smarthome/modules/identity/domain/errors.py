class IdentityError(Exception):
    """Base class for identity rule violations."""


class InvalidMembership(IdentityError):
    pass


class MembershipNotActive(IdentityError):
    pass


class InvalidInvitation(IdentityError):
    pass


class HomeNotFound(IdentityError):
    """Also raised when the caller is not a member, so homes cannot be enumerated."""


class AccessDenied(IdentityError):
    pass


class LastOwner(IdentityError):
    """A home must always keep at least one active owner."""


class AlreadyMember(IdentityError):
    pass
