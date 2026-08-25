class FoodLogError(Exception):
    """Base class for expected domain failures."""


class AccountCapacityReached(FoodLogError):
    pass


class AccountNotProvisioned(FoodLogError):
    pass


class AccountAlreadyProvisioned(FoodLogError):
    pass


class WaitlistUnavailable(FoodLogError):
    pass


class InboundAddressCollision(FoodLogError):
    pass


class InboundAddressStateConflict(FoodLogError):
    pass


class InboundAddressGenerationFailed(FoodLogError):
    pass


class DeviceCredentialCollision(FoodLogError):
    pass


class InvalidDeviceCredential(FoodLogError):
    pass


class CameraNotFound(FoodLogError):
    pass


class CrossAccountAccess(FoodLogError):
    pass


class TrialQuotaExhausted(FoodLogError):
    pass


class CaptureNotFound(FoodLogError):
    pass


class IdempotencyConflict(FoodLogError):
    pass


class JobIdentityConflict(FoodLogError):
    pass


class MealNotFound(FoodLogError):
    pass


class QuestionNotFound(FoodLogError):
    pass


class QuestionAlreadyAnswered(FoodLogError):
    pass
