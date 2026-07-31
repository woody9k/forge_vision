"""Request contracts for the HTTP API (FR-API-001, FR-API-007).

Every request body is a declared model rather than a bare dict, for three
reasons that matter more here than in an ordinary web service:

* **A typo must not become a silent default.** `extra="forbid"` means
  `{"medum": "soil_dry"}` is rejected rather than quietly ignored, which
  would otherwise return an air-medium result with no warning. Delivering a
  confidently wrong answer is the one thing this platform is built not to do.
* **Errors should name the field.** Without a model, a bad value surfaced as
  a leaked Python exception (`could not convert string to float: 'banana'`)
  and a missing key as a bare `KeyError`. FastAPI now returns a 422 saying
  which field and why.
* **The API describes itself.** These models are what makes `/openapi.json`
  usable by a client that has not read the source.

Field names and defaults mirror the runtime signatures exactly; the routes
stay thin.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    """Reject unknown fields everywhere."""

    model_config = ConfigDict(extra="forbid")


# -- devices ----------------------------------------------------------------
class RescanRequest(Strict):
    uri: str = Field("", description="Exact URI to open, e.g. ip:192.168.99.222. "
                                     "Bypasses the survey entirely.")
    prefer: str = Field("auto", pattern="^(auto|usb|network|usb-gadget|ip:.*|usb:.*)$",
                        description="Which transport to choose when surveying: "
                                    "auto (fastest measured), a kind, or a URI")
    measure: bool = Field(True, description="Time each transport before choosing. "
                                            "Set false for a faster, unmeasured scan.")


class RadioAddressRequest(Strict):
    """Where a radio lives, in whatever form a person would type it."""
    address: str = Field(..., min_length=1,
                         description="Hostname or IP (ip: prefix optional), "
                                     "or an explicit usb: URI")
    label: str = ""


class RadioAddressUpdateRequest(Strict):
    label: str | None = None
    address: str | None = None
    enabled: bool | None = None


class SwitchTransportRequest(Strict):
    """Reach the same radio a different way."""
    uri: str = Field(..., min_length=1)


class DeviceConfigRequest(Strict):
    center_frequency_hz: float | None = None
    sample_rate_hz: float | None = None
    rx_bandwidth_hz: float | None = None
    rx_gain_db: float | None = None
    tx_gain_db: float | None = None
    rx_channel: int | None = None
    tx_channel: int | None = None
    buffer_size: int | None = None

    def set_fields(self) -> dict:
        """Only what the caller actually supplied, so unset fields are kept."""
        return self.model_dump(exclude_none=True)


class TxRequest(Strict):
    enable: bool
    waveform: str = Field("", description="Required when enable is true")


# -- safety -----------------------------------------------------------------
class ArmRequest(Strict):
    operator: str = Field(..., min_length=1)
    acknowledgement: str = Field(..., min_length=1)


class ChecklistRequest(Strict):
    id: str = ""
    confirmed: bool = True
    reset: bool = False


class ProfileRequest(Strict):
    profile: str


class PathAttenuationRequest(Strict):
    attenuation_db: float = Field(..., ge=0,
                                  description="Attenuation/isolation between "
                                              "TX and RX. A physical claim.")


# -- acquisition ------------------------------------------------------------
class SurveyRequest(Strict):
    device_id: str = "sim-pluto-0"
    start_hz: float = 902e6
    stop_hz: float = 928e6
    step_hz: float = Field(2e6, gt=0)
    sample_rate_hz: float = Field(2.5e6, gt=0)
    rx_gain_db: float = 40.0
    samples: int = Field(65536, gt=0)
    name: str = "band survey"
    operator: str = ""


class RangeRunRequest(Strict):
    device_id: str = "sim-pluto-0"
    waveform: str = "fmcw_bench_56M"
    chirps: int = Field(8, gt=0)
    medium: str | dict | None = None
    use_background: bool = True
    name: str = "range run"
    operator: str = ""
    tags: list[str] | None = None
    pipeline_overrides: dict | None = None
    parent_id: str | None = None


class SteppedRunRequest(Strict):
    device_id: str = "sim-pluto-0"
    start_hz: float = 100e6
    stop_hz: float = 500e6
    waveform: str = "fmcw_pluto_40M"
    overlap: float = Field(0.5, ge=0, lt=1)
    chirps: int = Field(4, gt=0)
    medium: str | dict | None = None
    correction: str = Field("overlap", pattern="^(overlap|none)$")
    max_range_m: float = Field(20.0, gt=0)
    name: str = "stepped-frequency run"
    operator: str = ""


class CaptureRequest(Strict):
    device_id: str = "sim-pluto-0"
    num_samples: int = Field(262144, gt=0)
    segments: int = Field(1, gt=0)
    name: str = "raw capture"
    operator: str = ""
    waveform: str = ""
    tags: list[str] | None = None


class CableDelayRequest(Strict):
    delay_s: float = 0.0


class BackgroundRequest(Strict):
    waveform: str = "fmcw_bench_56M"
    chirps: int = Field(8, gt=0)
    operator: str = ""


# -- scanning ---------------------------------------------------------------
class ScanPlan(Strict):
    start_m: float = 0.0
    end_m: float = 3.0
    step_m: float = Field(0.1, gt=0)
    waveform: str = "fmcw_bench_56M"
    chirps: int = Field(4, gt=0)
    medium: str | dict = "soil_dry"
    antenna_height_m: float = 0.0
    orientation: str = "broadside"
    position_uncertainty_m: float = Field(0.01, ge=0)
    max_range_m: float = Field(16.0, gt=0)
    notes: str = ""


class ScanStartRequest(Strict):
    device_id: str = "sim-pluto-0"
    plan: ScanPlan = Field(default_factory=ScanPlan)
    operator: str = ""


class ScanPointRequest(Strict):
    x_m: float | None = Field(None, description="Omit to take the position "
                                                "from the active source")
    operator_override: bool = False


# -- experiments ------------------------------------------------------------
class AnnotationRequest(BaseModel):
    """Annotations are free-form by design: an operator's note should never be
    rejected for using a field the schema did not anticipate."""

    model_config = ConfigDict(extra="allow")
    type: str = "note"
    text: str = ""


class ReplayRequest(Strict):
    medium: str | dict | None = None
    pipeline_overrides: dict | None = None


# -- jobs -------------------------------------------------------------------
class JobRequest(Strict):
    kind: str = Field(..., pattern="^(survey|site_scene|replay)$")
    params: dict = Field(default_factory=dict)


# -- sites ------------------------------------------------------------------
class SiteRequest(Strict):
    name: str = Field(..., min_length=1)
    coordinate_system: str = ""
    notes: str = ""


class RegisterScanRequest(Strict):
    experiment_id: str = Field(..., min_length=1)
    origin_x_m: float = 0.0
    origin_y_m: float = 0.0
    heading_deg: float = 0.0
    label: str = ""
    position_uncertainty_m: float = Field(0.05, ge=0)


class UnregisterScanRequest(Strict):
    experiment_id: str = Field(..., min_length=1)


# -- RF chain and components ------------------------------------------------
class RfChainRequest(Strict):
    tx_ids: list[str] = Field(default_factory=list)
    rx_ids: list[str] = Field(default_factory=list)
    antenna_tx: str = ""
    antenna_rx: str = ""


class AdoptLossRequest(Strict):
    """Take nominal loss from an imported S21 sweep, at a stated frequency."""
    freq_hz: float | None = Field(None, gt=0)


class ChainConfigRequest(Strict):
    """Save the working chain as a reusable named configuration."""
    name: str = Field(min_length=1)
    notes: str = ""


class ComponentRequest(Strict):
    kind: str = "antenna"
    name: str = Field(..., min_length=1)
    connector: str = ""
    claimed_band: str = ""
    polarization: str = ""
    notes: str = ""
    nominal_loss_db: float | None = None
    nominal_delay_ns: float | None = None


class ComponentUpdateRequest(Strict):
    name: str | None = None
    connector: str | None = None
    claimed_band: str | None = None
    polarization: str | None = None
    notes: str | None = None
    nominal_loss_db: float | None = None
    nominal_delay_ns: float | None = None

    def set_fields(self) -> dict:
        return self.model_dump(exclude_none=True)


# -- positioning ------------------------------------------------------------
class PositionSourceRequest(Strict):
    kind: str = Field("manual", pattern="^(manual|serial|replay)$")
    port: str = ""
    baud: int = 115200
    wheel_circumference_m: float = Field(0.0, ge=0)
    counts_per_revolution: int = Field(0, ge=0)
    uncertainty_m: float = Field(0.01, ge=0)
    samples: list[dict] = Field(default_factory=list)

    def options(self) -> dict:
        return self.model_dump(exclude={"kind"})


# -- SAGE and narration -----------------------------------------------------
class SageAskRequest(Strict):
    question: str = ""
    site_id: str = ""
    experiment_id: str = ""
    narrate: bool = False


class LlmEndpointRequest(Strict):
    name: str = Field(..., min_length=1)
    base_url: str = Field(..., min_length=1)
    model: str = ""
    api_key: str = ""
    timeout_s: float = Field(60.0, gt=0)
    max_tokens: int = Field(700, gt=0)
    enabled: bool = False


# -- simulator --------------------------------------------------------------
class SimCapsRequest(Strict):
    profile: str = Field(..., pattern="^(pluto_plus|pluto_rev_b)$")


class SimSceneRequest(Strict):
    preset: str = ""
    targets: list[dict] | None = None
    medium: str | dict | None = None
    noise_floor_dbfs: float | None = None
    leakage_amplitude: float | None = None
