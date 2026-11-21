"""OUROBOROS — Motor unificado de engenharia temporal, análise perceptiva e síntese de loops PS4.

Substitui os motores concorrentes legados por módulos internos especializados coordenados
por um único orquestrador público (`OuroborosEngine`).
"""

from __future__ import annotations

OUROBOROS_VERSION = "ouroboros-absolute-1"

PROPOSAL_PROFILES = (
    {
        "id": "quality",
        "order": 1,
        "title": "PROPOSTA 1 — FIDELIDADE MÁXIMA — 720p",
        "shortTitle": "Fidelidade Máxima",
        "size": (1280, 720),
        "resolutionIdx": 2,
        "priority": "Definição, nitidez, preservação de detalhes e fidelidade visual",
    },
    {
        "id": "balance",
        "order": 2,
        "title": "PROPOSTA 2 — MOVIMENTO NATURAL — 540p",
        "shortTitle": "Movimento Natural",
        "size": (960, 540),
        "resolutionIdx": 1,
        "priority": "Continuidade, definição, duração, cadência e naturalidade",
    },
    {
        "id": "flexibility",
        "order": 3,
        "title": "PROPOSTA 3 — CONTINUIDADE TEMPORAL — 360p",
        "shortTitle": "Continuidade Temporal",
        "size": (640, 360),
        "resolutionIdx": 0,
        "priority": "Quantidade útil de quadros, regularidade temporal e movimento contínuo",
    },
)

INTERNAL_STRATEGIES = {
    "A": ("natural", "Corte natural (intervalo contínuo sem reconstrução artificial)"),
    "B": ("seam_correction", "Correção de junção (harmonização fotométrica e ponte de conteúdo)"),
    "C": ("motion_compensation", "Compensação de movimento (fluxo óptico e warping direcional)"),
    "D": ("regional_reconstruction", "Reconstrução regional (preserva áreas estáticas e corrige áreas dinâmicas)"),
    "E": ("periodic_synthesis", "Síntese periódica (advecção Euleriana 1,0× anti-ghosting)"),
    "F": ("post_dds_finish", "Acabamento pós-DDS (validação e refino na sequência DXT1a real)"),
}

from .orchestrator import OuroborosEngine  # noqa: E402

__all__ = [
    "OUROBOROS_VERSION",
    "PROPOSAL_PROFILES",
    "INTERNAL_STRATEGIES",
    "OuroborosEngine",
]
