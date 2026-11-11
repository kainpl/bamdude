"""The MQTT thread's frozen physical-run boundary, shared with async consumers."""

from dataclasses import dataclass


def run_id(value) -> str | None:
    value = str(value or "").strip()
    return value if value and value != "0" else None


@dataclass(frozen=True)
class PrintObservation:
    verdict: str
    previous_id: str | None
    current_id: str | None
    previous_state: str | None
    generation: int
    lineage: str | None = None
    layer_identity_revision: int | None = None

    @property
    def new_run(self) -> bool:
        return self.verdict in {"observed_new_start", "different_client_run_id"}


def observation_is_current(manager, printer_id, observation):
    if observation is None:
        return True
    if not isinstance(observation, PrintObservation):
        return False
    client = getattr(manager, "_clients", {}).get(printer_id)
    return (
        client is not None
        and client._run_boundary_generation == observation.generation
        and (
            observation.lineage == client._print_lineage
            if observation.lineage is not None
            else observation == client._start_observation
        )
    )
