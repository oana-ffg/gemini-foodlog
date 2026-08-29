class FoodLogError(Exception):
    """Base class for expected domain failures."""


class AccountCapacityReached(FoodLogError):
    pass


class AccountCapacityStateConflict(FoodLogError):
    pass


class AccountNotProvisioned(FoodLogError):
    pass


class AccountAlreadyProvisioned(FoodLogError):
    pass


class AccountExportAlreadyActive(FoodLogError):
    def __init__(self, export_id: str) -> None:
        super().__init__(export_id)
        self.export_id = export_id


class AccountExportNotFound(FoodLogError):
    pass


class AccountExportLeaseActive(FoodLogError):
    pass


class AccountExportRateLimited(FoodLogError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(retry_after_seconds)
        self.retry_after_seconds = retry_after_seconds


class WaitlistUnavailable(FoodLogError):
    pass


class WaitlistEntryNotFound(FoodLogError):
    pass


class InboundAddressCollision(FoodLogError):
    pass


class InboundAddressStateConflict(FoodLogError):
    pass


class InboundAddressGenerationFailed(FoodLogError):
    pass


class RawMailNotFound(FoodLogError):
    pass


class RawMailAuthenticationConflict(FoodLogError):
    pass


class RawMailProcessingConflict(FoodLogError):
    pass


class PurchaseIdentityConflict(FoodLogError):
    pass


class PurchaseDocumentConflict(FoodLogError):
    pass


class PurchaseNormalizationConflict(FoodLogError):
    pass


class PurchaseNotFound(FoodLogError):
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


class ActivityEventNotFound(FoodLogError):
    pass


class ModelSpendLimitExceeded(FoodLogError):
    pass


class ModelSpendReservationConflict(FoodLogError):
    pass


class ModelUsageConflict(FoodLogError):
    pass


class ModelUsageExceedsReservation(FoodLogError):
    pass


class ModelInvocationAlreadyReconciled(FoodLogError):
    pass


class AiTraceConflict(FoodLogError):
    pass


class AiTraceNotFound(FoodLogError):
    pass


class UserContextNoteNotFound(FoodLogError):
    pass


class IdempotencyConflict(FoodLogError):
    pass


class JobIdentityConflict(FoodLogError):
    pass


class MealNotFound(FoodLogError):
    pass


class MealRevisionConflict(FoodLogError):
    pass


class InvalidMealCorrectionTarget(FoodLogError):
    pass


class InvalidMealFeedbackTransition(FoodLogError):
    pass


class KnowledgePageNotFound(FoodLogError):
    pass


class KnowledgeRevisionConflict(FoodLogError):
    pass


class InvalidKnowledgeProvenance(FoodLogError):
    pass


class InvalidKnowledgeTransition(FoodLogError):
    pass


class QuestionNotFound(FoodLogError):
    pass


class QuestionAlreadyAnswered(FoodLogError):
    pass


class QuestionSuperseded(FoodLogError):
    pass
