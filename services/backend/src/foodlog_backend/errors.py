class FoodLogError(Exception):
    """Base class for expected domain failures."""


class AccountCapacityReached(FoodLogError):
    pass


class AccountNotProvisioned(FoodLogError):
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


class MealNotFound(FoodLogError):
    pass


class QuestionNotFound(FoodLogError):
    pass


class QuestionAlreadyAnswered(FoodLogError):
    pass
