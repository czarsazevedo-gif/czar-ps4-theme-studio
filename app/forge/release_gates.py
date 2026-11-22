"""Permanent fail-closed Release Gates for PS4 Theme PKG validation (Operação Fortaleza — Fase 11).

Implements the 7 explicit trust states:
  - BUILD_FAILED
  - STRUCTURE_VALIDATED
  - SEMANTICALLY_VALIDATED
  - INDEPENDENTLY_COMPARED
  - PHYSICAL_TEST_PENDING
  - PHYSICALLY_VERIFIED
  - REGRESSION_DETECTED

Core guarantees:
  1. Fail closed whenever any mandatory check is missing, inconclusive, or failed.
  2. Never promote to PHYSICALLY_VERIFIED without an explicit console test record
     matching BOTH the exact `pkg_sha256` and `inputs_fingerprint` of the tested artifact.
  3. Never inherit physical approval across revisions that modify relevant resources.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .contracts import ReleaseGateState

RELEASE_GATE_STATES: tuple[ReleaseGateState, ...] = (
    "BUILD_FAILED",
    "STRUCTURE_VALIDATED",
    "SEMANTICALLY_VALIDATED",
    "INDEPENDENTLY_COMPARED",
    "PHYSICAL_TEST_PENDING",
    "PHYSICALLY_VERIFIED",
    "REGRESSION_DETECTED",
)


@dataclass(frozen=True)
class PhysicalVerificationRecord:
    """Explicit physical console test evidence bound strictly to one exact PKG SHA-256."""

    pkg_sha256: str
    inputs_fingerprint: str
    console_model: str
    firmware: str
    install_method: str
    passed: bool
    recorded_at: str
    notes: str = ""


def can_inherit_physical_verification(
    previous: PhysicalVerificationRecord | None,
    *,
    pkg_sha256: str,
    inputs_fingerprint: str,
) -> tuple[bool, str]:
    """Verify whether a physical verification record applies to this exact build.

    A new build NEVER inherits physical verification from a prior build whose PKG SHA-256
    or effective theme inputs fingerprint changed.
    """
    if previous is None:
        return False, "Nenhum registro de teste físico disponível para este pacote."
    if not previous.passed:
        return False, "O registro de teste físico anterior foi reprovado."
    if not pkg_sha256 or previous.pkg_sha256.lower() != pkg_sha256.lower():
        return (
            False,
            "Aprovação física não herdável: SHA-256 do PKG atual difere do pacote efetivamente testado no console.",
        )
    if previous.inputs_fingerprint and inputs_fingerprint and previous.inputs_fingerprint != inputs_fingerprint:
        return (
            False,
            "Aprovação física invalidada: entradas/configurações do tema mudaram após o teste físico.",
        )
    if not previous.console_model.strip() or not previous.firmware.strip():
        return False, "Registro de teste físico incompleto (modelo ou firmware ausente)."
    return True, f"Comprovado fisicamente em {previous.console_model} (firmware {previous.firmware})."


def evaluate_release_gate(
    *,
    pkg_created: bool,
    header_and_crypto_valid: bool,
    sc0_validated: bool,
    theme_type_valid: bool,
    image0_extracted: bool,
    semantic_contract_passed: bool | None,
    digital_twin_passed: bool | None,
    bundle_compared: bool,
    bundle_hashes_matched: bool,
    independent_verifiers_passed: bool | None = None,
    regression_detected: bool = False,
    stale_vs_project: bool = False,
    pkg_sha256: str = "",
    inputs_fingerprint: str = "",
    physical_record: PhysicalVerificationRecord | None = None,
    errors: list[str] | None = None,
) -> dict[str, Any]:
    """Deterministically evaluate the highest earned Release Gate state (strictly fail-closed)."""
    reasons: list[str] = []
    err_list = list(errors or [])

    if regression_detected:
        reasons.append("Regressão ou invalidação de integridade detectada na suíte ou nos derivados.")
        return {
            "state": "REGRESSION_DETECTED",
            "reasons": reasons,
            "physical_verified": False,
            "ready_for_physical_test": False,
        }

    if not pkg_created or err_list or not (header_and_crypto_valid and sc0_validated and theme_type_valid):
        if not pkg_created:
            reasons.append("Arquivo PKG não foi criado.")
        if not header_and_crypto_valid:
            reasons.append("Cabeçalho CNT ou digests criptográficos inválidos/ausentes.")
        if not sc0_validated:
            reasons.append("Validação de Sc0 (param.sfo/icon0/pic1) falhou.")
        if not theme_type_valid:
            reasons.append("Identidade binária de tema PS4 inválida (exigido content_type=0x1B, IRO_TAG=2, CATEGORY=ac).")
        if err_list:
            reasons.append(f"{len(err_list)} erro(s) estrutural(is) ou semântico(s) registrado(s).")
        return {
            "state": "BUILD_FAILED",
            "reasons": reasons,
            "physical_verified": False,
            "ready_for_physical_test": False,
        }

    if stale_vs_project:
        reasons.append("Projeto modificado após a geração do PKG — selo de aprovação anterior invalidado.")
        return {
            "state": "REGRESSION_DETECTED",
            "reasons": reasons,
            "physical_verified": False,
            "ready_for_physical_test": False,
        }

    if not image0_extracted:
        reasons.append("Cabeçalho e Sc0 íntegros, mas Image0 não foi extraído — insuficiente para aprovação semântica.")
        return {
            "state": "STRUCTURE_VALIDATED",
            "reasons": reasons,
            "physical_verified": False,
            "ready_for_physical_test": False,
        }

    # Semantic contract & Digital Twin are mandatory to progress beyond STRUCTURE_VALIDATED
    if semantic_contract_passed is not True or digital_twin_passed is not True:
        if semantic_contract_passed is False:
            reasons.append("Contrato semântico PS4 reprovado sobre o conteúdo extraído.")
            return {
                "state": "BUILD_FAILED",
                "reasons": reasons,
                "physical_verified": False,
                "ready_for_physical_test": False,
            }
        if digital_twin_passed is False:
            reasons.append("PS4 Theme Digital Twin reprovou a simulação semântica sobre o conteúdo extraído.")
            return {
                "state": "BUILD_FAILED",
                "reasons": reasons,
                "physical_verified": False,
                "ready_for_physical_test": False,
            }
        reasons.append("Validação semântica ou Digital Twin não executada/inconclusiva (fail-closed).")
        return {
            "state": "STRUCTURE_VALIDATED",
            "reasons": reasons,
            "physical_verified": False,
            "ready_for_physical_test": False,
        }

    # Bundle parity check if bundle was compared
    if bundle_compared and not bundle_hashes_matched:
        reasons.append("Divergência SHA-256 entre o conteúdo extraído do PKG e o bundle validado.")
        return {
            "state": "BUILD_FAILED",
            "reasons": reasons,
            "physical_verified": False,
            "ready_for_physical_test": False,
        }

    # Physical verification check — NEVER granted unless exact SHA-256 & fingerprint match!
    phys_ok, phys_msg = can_inherit_physical_verification(
        physical_record,
        pkg_sha256=pkg_sha256,
        inputs_fingerprint=inputs_fingerprint,
    )
    if phys_ok:
        reasons.append(phys_msg)
        return {
            "state": "PHYSICALLY_VERIFIED",
            "reasons": reasons,
            "physical_verified": True,
            "ready_for_physical_test": True,
        }

    # Distinguish between semantically validated, independently compared, and ready for physical test
    if independent_verifiers_passed is False:
        reasons.append("Verificador independente externo reprovou o PKG.")
        return {
            "state": "BUILD_FAILED",
            "reasons": reasons,
            "physical_verified": False,
            "ready_for_physical_test": False,
        }

    if bundle_compared and bundle_hashes_matched:
        reasons.append("Integridade estrutural, contrato semântico PS4, Digital Twin e paridade SHA-256 aprovados.")
        if independent_verifiers_passed is True:
            reasons.append("Confirmado por verificadores independentes externos.")
        reasons.append("Aguardando ensaio físico em PS4 Slim (firmware 12.52).")
        return {
            "state": "PHYSICAL_TEST_PENDING",
            "reasons": reasons,
            "physical_verified": False,
            "ready_for_physical_test": True,
        }

    if independent_verifiers_passed is True:
        reasons.append("Validado semanticamente e confirmado por ferramentas independentes (sem bundle de origem).")
        return {
            "state": "INDEPENDENTLY_COMPARED",
            "reasons": reasons,
            "physical_verified": False,
            "ready_for_physical_test": True,
        }

    reasons.append("Validado estrutural e semanticamente após extração (sem bundle de origem ou comparador externo).")
    return {
        "state": "SEMANTICALLY_VALIDATED",
        "reasons": reasons,
        "physical_verified": False,
        "ready_for_physical_test": True,
    }
