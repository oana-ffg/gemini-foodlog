from .operational_logging import emit_operational_event


def main() -> None:
    """Fail visibly if Cloud Run ever injects a notification-only secret here."""
    emit_operational_event(
        "ERROR",
        "secret_access_boundary_broken",
        service="secret_denial_smoke",
        outcome="unexpected_container_start",
    )
    raise RuntimeError("notification-only secret reached a forbidden runtime identity")


if __name__ == "__main__":
    main()
