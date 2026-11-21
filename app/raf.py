"""Generador de la escena RAF (scene/background.raf/index.xml).

Un fondo animado de PS4 no es una lista de imagenes: es una escena 3D. La
animacion se hace con un unico plano compartido, un material por frame, un
actor por frame apilados en el mismo sitio, y una secuencia que va cambiando
el alpha de cada actor para pasar de uno al siguiente.

    <model id="plane" file="plane.fbx"/>              <- el .mdx que generamos
    <material id="f00" effect="pure_texture">
        <texture file="00.dds"/>
    </material>
    <actor id="f00" model="plane" material="f00" color="1 1 1 1" .../>
    <actor id="f01" model="plane" material="f01" color="1 1 1 0" .../>
    <sequence>
        <case wait="0.06">
            <actor id="f00" color="1 1 1 0"/>
            <actor id="f01" color="1 1 1 1"/>
        </case>
    </sequence>

Dos detalles que no se adivinan:

* El XML referencia el modelo como `.fbx` aunque el archivo en disco sea
  `.mdx`. Asi lo hace el tema de referencia.
* `wait` va en SEGUNDOS y admite decimales (0.06 = ~16 fps). No es una unidad
  de 100ms como dicen algunos tutoriales.
"""

from __future__ import annotations

import math

#: Name of the model shared by every frame.
MODEL_ID = "plane"
MODEL_FILE = "plane.fbx"
MODEL_REAL_FILE = "plane.mdx"

#: The reference theme's camera. The plane is sized to fill this FOV.
CAMERA_YFOV = 29.0
CAMERA_ZNEAR = 0.1
CAMERA_ZFAR = 300.0

#: Plane-to-camera distance, in scene units (depth * scale).
PLANE_DEPTH = 2.146484
PLANE_SCALE = 100.0

#: No margin: the plane fits the field of view exactly.
#:
#: The reference theme does overshoot in width (101.56 against the 98.7 of an
#: 16:9 exacto), pero puede permitirselo porque sus UV recortan un
#: sub-rectangle of the texture at that same ratio. We map the whole image
#: (UV 0..1), so enlarging the plane would stretch it.
OVERSCAN_X = 1.0

#: Screen aspect ratio.
ASPECT = 16 / 9

#: Depth the actors are stacked at (taken from the reference theme).
ACTOR_Z = 9


def plane_half_size() -> tuple[float, float]:
    """Medio ancho y medio alto del plano, en unidades locales del modelo.

    Se calcula para que llene justo el campo de vision vertical de la camara
    a la distancia a la que esta el plano.
    """
    world_depth = PLANE_DEPTH * PLANE_SCALE
    half_h_world = world_depth * math.tan(math.radians(CAMERA_YFOV / 2))
    half_w_world = half_h_world * ASPECT * OVERSCAN_X
    return half_w_world / PLANE_SCALE, half_h_world / PLANE_SCALE


def frame_id(index: int) -> str:
    return f"f{index:02d}"


def build_scene(frame_files: list[str], wait_seconds: float, *, wait_list: list[float] | None = None) -> str:
    """Arma el index.xml de la escena para una secuencia de texturas.

    `frame_files` son los nombres de los .dds dentro de la carpeta .raf.
    Si se proporciona `wait_list` (longitud == len(frame_files)), asigna un `wait`
    individual por `<case>` (Asignación Temporal No Uniforme por longitud de arco cinemático),
    conservando la duración total exacta del ciclo.
    """
    if not frame_files:
        raise ValueError("at least one frame is required")
    wait = max(0.01, float(wait_seconds))
    count = len(frame_files)
    if wait_list is not None and len(wait_list) == count:
        waits = [max(0.01, round(float(w_i), 4)) for w_i in wait_list]
    else:
        waits = [wait] * count

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<raf angle_unit="degree">',
        '',
        '\t<system camera_switch_mode="vertical" layer_switch_distance="0"/>',
        '',
        f'\t<camera id="camera1" type="perspective" yfov="{CAMERA_YFOV:g}"'
        f' znear="{CAMERA_ZNEAR:g}" zfar="{CAMERA_ZFAR:g}"'
        ' position="0 0 0" direction="0 0 -1" up="0 1 0"/>',
        '\t<light id="light1" type="ambient" color="1 1 1"/>',
        '',
        f'\t<model id="{MODEL_ID}" file="{MODEL_FILE}"/>',
        '',
    ]

    for index, name in enumerate(frame_files):
        fid = frame_id(index)
        lines += [
            f'\t<material id="{fid}" effect="pure_texture">',
            f'\t\t<texture file="{name}"/>',
            '\t</material>',
        ]
    lines.append('')

    # Only the first actor starts visible; the rest at alpha 0.
    for index in range(len(frame_files)):
        fid = frame_id(index)
        alpha = 1 if index == 0 else 0
        lines.append(
            f'\t<actor id="{fid}" model="{MODEL_ID}" material="{fid}"'
            f' color="1 1 1 {alpha}" position="0 0 {ACTOR_Z}" zsort="back_to_front"/>'
        )
    lines.append('')

    # The sequence fades the current frame out and the next one in. Closing
    # the cycle returns to the first, so the loop repeats on its own.
    lines.append('\t<sequence>')
    for index in range(count):
        nxt = (index + 1) % count
        w_case = waits[index]
        lines += [
            f'\t\t<case wait="{w_case:g}">',
            f'\t\t\t<actor id="{frame_id(index)}" color="1 1 1 0"/>',
            f'\t\t\t<actor id="{frame_id(nxt)}" color="1 1 1 1"/>',
            '\t\t</case>',
        ]
    lines.append('\t</sequence>')
    lines.append('')
    lines.append('</raf>')
    lines.append('')
    return "\n".join(lines)

