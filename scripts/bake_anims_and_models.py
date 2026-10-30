#!/usr/bin/env python3
"""
Bake vanilla Minecraft 26.3 keyframe animations (for mob-data.json) and
all LayerDefinitions (for entity-models.json) directly from /tmp/mc-26.3-src
using exact vanilla Cube/Polygon/PartPose/Keyframe math.
"""
import copy
import json
import math
import re
import struct
from pathlib import Path

MC_SRC = Path("/tmp/mc-26.3-src/src")


def f32(x: float) -> float:
    return struct.unpack("<f", struct.pack("<f", float(x)))[0]


# ---------------------------------------------------------------------------
# 1. Geometry & Model Builder Runtime (exact port of ModelPart.java & builders)
# ---------------------------------------------------------------------------

DIRECTIONS_ALL = ("DOWN", "UP", "NORTH", "SOUTH", "WEST", "EAST")
DIR_NORMALS = {
    "DOWN": (0.0, -1.0, 0.0),
    "UP": (0.0, 1.0, 0.0),
    "NORTH": (0.0, 0.0, -1.0),
    "SOUTH": (0.0, 0.0, 1.0),
    "WEST": (-1.0, 0.0, 0.0),
    "EAST": (1.0, 0.0, 0.0),
}
DIR_OPPOSITE_X = {
    "WEST": "EAST",
    "EAST": "WEST",
    "DOWN": "DOWN",
    "UP": "UP",
    "NORTH": "NORTH",
    "SOUTH": "SOUTH",
}


class CubeDeformation:
    def __init__(self, gx=0.0, gy=None, gz=None):
        if gy is None:
            gy = gx
            gz = gx
        self.growX = f32(gx)
        self.growY = f32(gy)
        self.growZ = f32(gz)

    def extend(self, x, y=None, z=None):
        if y is None:
            return CubeDeformation(self.growX + x, self.growY + x, self.growZ + x)
        return CubeDeformation(self.growX + x, self.growY + y, self.growZ + z)


CubeDeformation.NONE = CubeDeformation(0.0)


class PartPose:
    def __init__(self, x=0.0, y=0.0, z=0.0, xRot=0.0, yRot=0.0, zRot=0.0, xScale=1.0, yScale=1.0, zScale=1.0):
        self.x = f32(x)
        self.y = f32(y)
        self.z = f32(z)
        self.xRot = f32(xRot)
        self.yRot = f32(yRot)
        self.zRot = f32(zRot)
        self.xScale = f32(xScale)
        self.yScale = f32(yScale)
        self.zScale = f32(zScale)

    @staticmethod
    def offset(x, y, z):
        return PartPose(x, y, z, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0)

    @staticmethod
    def rotation(xr, yr, zr):
        return PartPose(0.0, 0.0, 0.0, xr, yr, zr, 1.0, 1.0, 1.0)

    @staticmethod
    def offsetAndRotation(x, y, z, xr, yr, zr):
        return PartPose(x, y, z, xr, yr, zr, 1.0, 1.0, 1.0)

    def translated(self, dx, dy, dz):
        return PartPose(self.x + dx, self.y + dy, self.z + dz, self.xRot, self.yRot, self.zRot, self.xScale, self.yScale, self.zScale)

    def withScale(self, s):
        return PartPose(self.x, self.y, self.z, self.xRot, self.yRot, self.zRot, s, s, s)

    def scaled(self, sx, sy=None, sz=None):
        if sy is None:
            sy = sx
            sz = sx
        return PartPose(
            self.x * sx, self.y * sy, self.z * sz,
            self.xRot, self.yRot, self.zRot,
            self.xScale * sx, self.yScale * sy, self.zScale * sz,
        )


PartPose.ZERO = PartPose()


class CubeDefinition:
    def __init__(self, u, v, ox, oy, oz, dx, dy, dz, deform: CubeDeformation, mirror: bool, tx_scale=1.0, ty_scale=1.0, visible_faces=None):
        self.u = int(u)
        self.v = int(v)
        self.ox = f32(ox)
        self.oy = f32(oy)
        self.oz = f32(oz)
        self.dx = f32(dx)
        self.dy = f32(dy)
        self.dz = f32(dz)
        self.deform = deform
        self.mirror = bool(mirror)
        self.tx_scale = f32(tx_scale)
        self.ty_scale = f32(ty_scale)
        self.visible_faces = set(DIRECTIONS_ALL if visible_faces is None else visible_faces)

    def bake(self, tex_w: int, tex_h: int):
        minX = f32(self.ox - self.deform.growX)
        minY = f32(self.oy - self.deform.growY)
        minZ = f32(self.oz - self.deform.growZ)
        maxX = f32(self.ox + self.dx + self.deform.growX)
        maxY = f32(self.oy + self.dy + self.deform.growY)
        maxZ = f32(self.oz + self.dz + self.deform.growZ)
        if self.mirror:
            minX, maxX = maxX, minX

        t0 = (minX, minY, minZ)
        t1 = (maxX, minY, minZ)
        t2 = (maxX, maxY, minZ)
        t3 = (minX, maxY, minZ)
        l0 = (minX, minY, maxZ)
        l1 = (maxX, minY, maxZ)
        l2 = (maxX, maxY, maxZ)
        l3 = (minX, maxY, maxZ)

        xTexSize = f32(tex_w * self.tx_scale)
        yTexSize = f32(tex_h * self.ty_scale)
        w = self.dx
        h = self.dy
        d = self.dz
        u0 = float(self.u)
        u1 = u0 + d
        u2 = u0 + d + w
        u22 = u0 + d + w + w
        u3 = u0 + d + w + d
        u4 = u0 + d + w + d + w
        v0 = float(self.v)
        v1 = v0 + d
        v2 = v0 + d + h

        face_specs = [
            ("DOWN", [l1, l0, t0, t1], u1, v0, u2, v1),
            ("UP", [t2, t3, l3, l2], u2, v1, u22, v0),
            ("WEST", [t0, l0, l3, t3], u0, v1, u1, v2),
            ("NORTH", [t1, t0, t3, t2], u1, v1, u2, v2),
            ("EAST", [l1, t1, t2, l2], u2, v1, u3, v2),
            ("SOUTH", [l0, l1, l2, l3], u3, v1, u4, v2),
        ]
        polygons = []
        normals = []
        for facing, pts, fu0, fv0, fu1, fv1 in face_specs:
            if facing not in self.visible_faces:
                continue
            uvs = [
                (fu1 / xTexSize if xTexSize else 0.0, fv0 / yTexSize if yTexSize else 0.0),
                (fu0 / xTexSize if xTexSize else 0.0, fv0 / yTexSize if yTexSize else 0.0),
                (fu0 / xTexSize if xTexSize else 0.0, fv1 / yTexSize if yTexSize else 0.0),
                (fu1 / xTexSize if xTexSize else 0.0, fv1 / yTexSize if yTexSize else 0.0),
            ]
            verts = [
                [
                    f32(p[0]),
                    f32(p[1]),
                    f32(p[2]),
                    f32(uv[0]) if math.isfinite(uv[0]) else 0.0,
                    f32(uv[1]) if math.isfinite(uv[1]) else 0.0,
                ]
                for p, uv in zip(pts, uvs)
            ]
            n_dir = DIR_OPPOSITE_X[facing] if self.mirror else facing
            if self.mirror:
                verts.reverse()
            polygons.append(verts)
            normals.append(list(DIR_NORMALS[n_dir]))
        return polygons, normals


class CubeListBuilder:
    def __init__(self):
        self.cubes = []
        self.xTexOffs = 0
        self.yTexOffs = 0
        self._mirror = False

    @staticmethod
    def create():
        return CubeListBuilder()

    def texOffs(self, u, v):
        self.xTexOffs = int(u)
        self.yTexOffs = int(v)
        return self

    def mirror(self, m=True):
        self._mirror = bool(m)
        return self

    def addBox(self, *args):
        # Overloads in CubeListBuilder.java:
        # (comment, ox, oy, oz, dx, dy, dz, ...) -> drop leading str comment
        if args and isinstance(args[0], str):
            args = args[1:]
        if len(args) == 6:
            ox, oy, oz, dx, dy, dz = args
            self.cubes.append(CubeDefinition(self.xTexOffs, self.yTexOffs, ox, oy, oz, dx, dy, dz, CubeDeformation.NONE, self._mirror))
        elif len(args) == 7:
            ox, oy, oz, dx, dy, dz, arg7 = args
            if isinstance(arg7, CubeDeformation):
                self.cubes.append(CubeDefinition(self.xTexOffs, self.yTexOffs, ox, oy, oz, dx, dy, dz, arg7, self._mirror))
            elif isinstance(arg7, bool):
                self.cubes.append(CubeDefinition(self.xTexOffs, self.yTexOffs, ox, oy, oz, dx, dy, dz, CubeDeformation.NONE, arg7))
            elif isinstance(arg7, (set, frozenset, list, tuple)):
                self.cubes.append(CubeDefinition(self.xTexOffs, self.yTexOffs, ox, oy, oz, dx, dy, dz, CubeDeformation.NONE, self._mirror, 1.0, 1.0, arg7))
        elif len(args) == 8:
            ox, oy, oz, dx, dy, dz, deform, arg8 = args
            if isinstance(deform, CubeDeformation) and isinstance(arg8, bool):
                self.cubes.append(CubeDefinition(self.xTexOffs, self.yTexOffs, ox, oy, oz, dx, dy, dz, deform, arg8))
            elif isinstance(deform, CubeDeformation) and isinstance(arg8, (set, frozenset, list, tuple)):
                self.cubes.append(CubeDefinition(self.xTexOffs, self.yTexOffs, ox, oy, oz, dx, dy, dz, deform, self._mirror, 1.0, 1.0, arg8))
            else:
                self.cubes.append(CubeDefinition(self.xTexOffs, self.yTexOffs, ox, oy, oz, dx, dy, dz, CubeDeformation.NONE, self._mirror, deform, arg8))
        elif len(args) == 9:
            ox, oy, oz, dx, dy, dz, deform, txs, tys = args
            self.cubes.append(CubeDefinition(self.xTexOffs, self.yTexOffs, ox, oy, oz, dx, dy, dz, deform, self._mirror, txs, tys))
        return self


class PartDefinition:
    def __init__(self, cubes=None, partPose=None):
        self.cubes = list(cubes or [])
        self.partPose = partPose or PartPose.ZERO
        self.children = {}

    def addOrReplaceChild(self, name: str, builder, partPose: PartPose = None):
        if isinstance(builder, PartDefinition):
            child = copy.deepcopy(builder)
            if partPose is not None:
                child.partPose = partPose
        else:
            child = PartDefinition(builder.cubes, partPose or PartPose.ZERO)
        old = self.children.get(name)
        if old is not None:
            for k, v in old.children.items():
                if k not in child.children:
                    child.children[k] = v
        self.children[name] = child
        return child

    def getChild(self, name: str):
        if name not in self.children:
            self.children[name] = PartDefinition([], PartPose.ZERO)
        return self.children[name]

    def clearChild(self, name: str):
        child = PartDefinition([], PartPose.ZERO)
        self.children[name] = child
        return child

    def getChildren(self):
        return set(self.children.items())

    def transformed(self, fn):
        out = PartDefinition(copy.deepcopy(self.cubes), fn(self.partPose))
        for k, v in self.children.items():
            out.children[k] = v.transformed(fn)
        return out

    def clearRecursively(self):
        self.cubes = []
        for c in self.children.values():
            c.clearRecursively()
        return self

    def retainPartsAndChildren(self, allowed: set):
        new_children = {}
        for k, v in list(self.children.items()):
            if k in allowed:
                new_children[k] = v
            else:
                v.retainPartsAndChildren(allowed)
                if v.children or v.cubes:
                    new_children[k] = v
        self.cubes = []
        self.children = new_children
        return self

    def retainExactParts(self, allowed: set):
        new_children = {}
        for k, v in list(self.children.items()):
            if k not in allowed:
                v.cubes = []
            v.retainExactParts(allowed)
            if k in allowed or v.children or v.cubes:
                new_children[k] = v
        self.cubes = []
        self.children = new_children
        return self

    def bake(self, tex_w: int, tex_h: int):
        p = self.partPose
        polys = []
        norms = []
        for c in self.cubes:
            cp, cn = c.bake(tex_w, tex_h)
            polys.extend(cp)
            norms.extend(cn)
        kids = {k: v.bake(tex_w, tex_h) for k, v in self.children.items()}
        return {
            "pose": [p.x, p.y, p.z, p.xRot, p.yRot, p.zRot, p.xScale, p.yScale, p.zScale],
            "polygons": polys,
            "normals": norms,
            "children": kids,
        }


class MeshDefinition:
    def __init__(self, root=None):
        self.root = root or PartDefinition([], PartPose.ZERO)

    def getRoot(self):
        return self.root

    def transformed(self, fn):
        return MeshDefinition(self.root.transformed(fn))

    def apply(self, transformer):
        return transformer(self)


class LayerDefinition:
    def __init__(self, mesh: MeshDefinition, tex_w: int, tex_h: int):
        self.mesh = mesh
        self.tex_w = int(tex_w)
        self.tex_h = int(tex_h)

    @staticmethod
    def create(mesh: MeshDefinition, tex_w: int, tex_h: int):
        return LayerDefinition(copy.deepcopy(mesh), tex_w, tex_h)

    def apply(self, transformer):
        return LayerDefinition(transformer(copy.deepcopy(self.mesh)), self.tex_w, self.tex_h)

    def bakeRoot(self):
        return self.mesh.root.bake(self.tex_w, self.tex_h)


class MeshTransformer:
    IDENTITY = staticmethod(lambda mesh: mesh)

    @staticmethod
    def scaling(factor: float):
        factor = f32(factor)
        yOffset = f32(24.016 * (1.0 - factor))
        return lambda mesh: mesh.transformed(lambda pose: pose.scaled(factor).translated(0.0, yOffset, 0.0))


class BabyModelTransform:
    def __init__(self, scaleHead: bool, babyYHeadOffset: float, babyZHeadOffset: float, babyHeadScale: float, babyBodyScale: float, bodyYOffset: float, headParts: set):
        self.scaleHead = bool(scaleHead)
        self.babyYHeadOffset = f32(babyYHeadOffset)
        self.babyZHeadOffset = f32(babyZHeadOffset)
        self.babyHeadScale = f32(babyHeadScale)
        self.babyBodyScale = f32(babyBodyScale)
        self.bodyYOffset = f32(bodyYOffset)
        self.headParts = set(headParts)

    def __call__(self, mesh: MeshDefinition) -> MeshDefinition:
        headScale = f32(1.5 / self.babyHeadScale) if self.scaleHead else 1.0
        bodyScale = f32(1.0 / self.babyBodyScale)
        headTransform = lambda p: p.translated(0.0, self.babyYHeadOffset, self.babyZHeadOffset).scaled(headScale)
        bodyTransform = lambda p: p.translated(0.0, self.bodyYOffset, 0.0).scaled(bodyScale)
        out = MeshDefinition()
        for k, child in mesh.getRoot().getChildren():
            out.getRoot().addOrReplaceChild(
                k,
                child.transformed(headTransform if k in self.headParts else bodyTransform),
            )
        return out


class ArmorModelSet:
    def __init__(self, head, chest, legs, feet):
        self.head = head
        self.chest = chest
        self.legs = legs
        self.feet = feet

    def map(self, fn):
        return ArmorModelSet(fn(self.head), fn(self.chest), fn(self.legs), fn(self.feet))

    def putFrom(self, values: "ArmorModelSet", out_dict):
        for k, v in [(self.head, values.head), (self.chest, values.chest), (self.legs, values.legs), (self.feet, values.feet)]:
            if hasattr(out_dict, "put"):
                out_dict.put(k, v)
            else:
                out_dict[k] = v


# ---------------------------------------------------------------------------
# 2. Bake all Keyframe Animations from client/animation/definitions/*.java
# ---------------------------------------------------------------------------

def bake_animations() -> dict:
    anim_dir = MC_SRC / "net/minecraft/client/animation/definitions"
    animations = {}

    def degree_vec(x, y, z):
        c = math.pi / 180.0
        return [f32(x * c), f32(y * c), f32(z * c)]

    def pos_vec(x, y, z):
        return [f32(x), f32(-y), f32(z)]

    def scale_vec(x, y, z):
        return [f32(x - 1.0), f32(y - 1.0), f32(z - 1.0)]

    for jf in sorted(anim_dir.glob("*.java")):
        cls_name = jf.stem
        src = jf.read_text()
        # Remove comments
        src = re.sub(r"//.*", "", src)
        src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)

        # Find static final AnimationDefinition fields
        for m in re.finditer(
            r"public\s+static\s+final\s+AnimationDefinition\s+([A-Z0-9_]+)\s*=\s*AnimationDefinition\.Builder\.withLength\(([^)]+)\)(.*?)\.build\(\)\s*;",
            src,
            flags=re.S,
        ):
            field_name = m.group(1)
            length_expr = re.sub(r"(\d+(?:\.\d+)?)[fFdD]\b", r"\1", m.group(2))
            length = f32(eval(length_expr))
            chain = m.group(3)
            looping = ".looping()" in chain
            bones = {}
            for am in re.finditer(r'\.addAnimation\(\s*"([^"]+)"\s*,\s*new\s+AnimationChannel\((.*?)\)\s*\)', chain, flags=re.S):
                bone = am.group(1)
                body = am.group(2)
                t_m = re.search(r"Targets\.([A-Z_]+)", body)
                if not t_m:
                    continue
                target_raw = t_m.group(1)
                target = {"ROTATION": "rotate", "POSITION": "translate", "SCALE": "scale"}.get(target_raw, target_raw.lower())
                kfs = []
                for km in re.finditer(
                    r"new\s+Keyframe\(\s*([^,]+),\s*KeyframeAnimations\.(degreeVec|posVec|scaleVec)\(([^)]+)\)\s*,\s*AnimationChannel\.Interpolations\.([A-Z_]+)\s*\)",
                    body,
                ):
                    ts = f32(eval(re.sub(r"(\d+(?:\.\d+)?)[fFdD]\b", r"\1", km.group(1))))
                    fn_name = km.group(2)
                    xyz_src = re.sub(r"(\d+(?:\.\d+)?)[fFdD]\b", r"\1", km.group(3))
                    xyz = [float(eval(part.strip())) for part in xyz_src.split(",")]
                    vec = {"degreeVec": degree_vec, "posVec": pos_vec, "scaleVec": scale_vec}[fn_name](*xyz)
                    interp = km.group(4).lower()
                    kfs.append([ts, vec, vec, interp])
                bones.setdefault(bone, []).append({"target": target, "keyframes": kfs})
            animations[f"{cls_name}.{field_name}"] = {
                "length": length,
                "looping": looping,
                "bones": bones,
            }
    return animations


# ---------------------------------------------------------------------------
# 3. Parse PartNames + ModelLayers + Transpile Java Model Builders to Python
# ---------------------------------------------------------------------------

def parse_part_names() -> dict:
    pn_file = MC_SRC / "net/minecraft/client/model/geom/PartNames.java"
    src = pn_file.read_text()
    consts = {}
    for m in re.finditer(r'public\s+static\s+final\s+String\s+([A-Z0-9_]+)\s*=\s*"([^"]+)"\s*;', src):
        consts[m.group(1)] = m.group(2)

    class PartNamesObj:
        @staticmethod
        def tentacle(i):
            return f"tentacle{i}"

        @staticmethod
        def bodySegment(i):
            return f"segment{i}"

    for k, v in consts.items():
        setattr(PartNamesObj, k, v)
    return PartNamesObj, consts


def parse_model_layers() -> tuple:
    ml_file = MC_SRC / "net/minecraft/client/model/geom/ModelLayers.java"
    src = ml_file.read_text()
    layers = {}
    armor_sets = {}

    for m in re.finditer(r'([A-Z0-9_]+)\s*=\s*register\(\s*"([^"]+)"(?:\s*,\s*"([^"]+)")?\s*\)\s*;', src):
        name, path, layer = m.group(1), m.group(2), m.group(3) or "main"
        layers[name] = f"minecraft:{path}#{layer}"

    for m in re.finditer(r'([A-Z0-9_]+)\s*=\s*registerArmorSet\(\s*"([^"]+)"\s*\)\s*;', src):
        name, path = m.group(1), m.group(2)
        armor_sets[name] = ArmorModelSet(
            f"minecraft:{path}#helmet",
            f"minecraft:{path}#chestplate",
            f"minecraft:{path}#leggings",
            f"minecraft:{path}#boots",
        )

    # WoodType signs / hanging signs / beds / standing & wall signs
    wood_types = ["oak", "spruce", "birch", "acacia", "cherry", "jungle", "dark_oak", "pale_oak", "crimson", "warped", "mangrove", "bamboo"]
    # Also check WoodType.java in MC_SRC for any new 26.3 wood types (like poplar!)
    wt_file = MC_SRC / "net/minecraft/world/level/block/state/properties/WoodType.java"
    if wt_file.is_file():
        found_wt = re.findall(r'register\(new WoodType\("([a-z0-9_]+)"', wt_file.read_text())
        if found_wt:
            wood_types = found_wt

    return layers, armor_sets, wood_types


def strip_comments(code: str) -> str:
    out = []
    i = 0
    n = len(code)
    in_str = False
    while i < n:
        if in_str:
            if code[i] == "\\" and i + 1 < n:
                out.append(code[i : i + 2])
                i += 2
                continue
            if code[i] == '"':
                in_str = False
            out.append(code[i])
            i += 1
        else:
            if code[i] == '"':
                in_str = True
                out.append(code[i])
                i += 1
            elif code[i : i + 2] == "//":
                while i < n and code[i] != "\n":
                    i += 1
            elif code[i : i + 2] == "/*":
                i += 2
                while i + 1 < n and code[i : i + 2] != "*/":
                    i += 1
                i += 2
            else:
                out.append(code[i])
                i += 1
    return "".join(out)


def extract_brace_block(src: str, open_idx: int) -> tuple[str, int]:
    depth = 0
    i = open_idx
    in_str = False
    while i < len(src):
        ch = src[i]
        if in_str:
            if ch == "\\" and i + 1 < len(src):
                i += 2
                continue
            if ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return src[open_idx + 1 : i], i + 1
        i += 1
    raise ValueError("Unbalanced braces")


def transpile_expr(expr: str, cls_name: str, local_methods: set, local_consts: set) -> str:
    s = expr.strip()
    # Strip Java float/long suffixes like 1.5F, 0.0f, 2L (not inside strings or hex)
    s = re.sub(r"(?<![A-Za-z0-9_])(\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)[fFdDlL]\b", r"\1", s)
    s = re.sub(r"\bnew\s+(CubeDeformation|PartPose|MeshDefinition|BabyModelTransform|ArmorModelSet|CubeListBuilder)(?:<[^>]*>)?\s*\(", r"\1(", s)
    s = re.sub(r"\btrue\b", "True", s)
    s = re.sub(r"\bfalse\b", "False", s)
    s = re.sub(r"\bnull\b", "None", s)
    s = re.sub(r"\bMath\.(PI|sin|cos|sqrt|abs|min|max|toRadians)\b", lambda m: f"Mth.{m.group(1)}", s)
    s = re.sub(r"\(float\)\s*", "", s)
    s = re.sub(r"\(double\)\s*", "", s)
    s = re.sub(r"\(int\)\s*", "int(", s) if False else s  # handled carefully below
    s = re.sub(r"\(int\)\s*\(([^()]+)\)", r"int(\1)", s)
    s = re.sub(r"\(int\)\s*([A-Za-z0-9_.]+)", r"int(\1)", s)
    # EnumSet.of / Set.of
    s = re.sub(r"\bEnumSet\.of\s*\(", "make_set(", s)
    s = re.sub(r"\bEnumSet\.allOf\s*\(\s*Direction\.class\s*\)", "set(DIRECTIONS_ALL)", s)
    s = s.replace("Direction.class", "Direction")
    s = re.sub(r"\bSet\.of\s*\(", "make_set(", s)
    # Sized array allocation: `new float[7]` or `new int[4]`
    s = re.sub(r"\bnew\s+(?:float|double)\s*\[\s*([^\]]+)\s*\]", r"([0.0] * (\1))", s)
    s = re.sub(r"\bnew\s+(?:int|long)\s*\[\s*([^\]]+)\s*\]", r"([0] * (\1))", s)
    # Block lambdas: `x -> { a; return b; }` or `x -> { a; b; return c; }`
    def _blk_lambda(m):
        param = m.group(1)
        inner = [p.strip() for p in m.group(2).split(";") if p.strip()]
        ret = inner[-1][7:].strip() if inner and inner[-1].startswith("return ") else "None"
        acts = inner[:-1] if inner and inner[-1].startswith("return ") else inner
        joined = ", ".join(acts + [ret])
        return f"lambda {param}: ({joined})[-1]"
    s = re.sub(r"(?:\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)|\b([A-Za-z_][A-Za-z0-9_]*))\s*->\s*\{([^{}]+)\}",
               lambda m: _blk_lambda(type("M", (), {"group": lambda _, i: (m.group(1) or m.group(2)) if i == 1 else m.group(3)})()), s)
    # Lambdas: `x -> expr` or `(x) -> expr`
    s = re.sub(r"\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)\s*->\s*", r"lambda \1: ", s)
    s = re.sub(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*->\s*", r"lambda \1: ", s)
    # Java Function.apply: `baseFactory.apply(arg)` where baseFactory is a parameter/callable
    s = re.sub(r"\b(baseFactory|factory|transformer)\.apply\s*\(", r"\1(", s)
    # Method references: `PartPose::offset` or `MeshDefinition::new`
    s = re.sub(r"\b([A-Za-z0-9_.]+)::new\b", r"\1", s)
    s = re.sub(r"\b([A-Za-z0-9_.]+)::([A-Za-z0-9_]+)\b", r"\1.\2", s)
    # Ternary `cond ? a : b` (single-level non-lambda)
    while "?" in s:
        # Match innermost `cond ? tr : fa` not crossing parens/commas
        m = re.search(r"([^?:,()]+)\?\s*([^?:,]+?)\s*:\s*([^?:,)]+)", s)
        if not m:
            break
        cond, tr, fa = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        s = s[: m.start()] + f"({tr} if {cond} else {fa})" + s[m.end() :]
    s = s.replace("&&", " and ").replace("||", " or ")
    s = re.sub(r"!(?!=)", " not ", s)
    # Qualify bare local static calls / constants on cls_name
    for mname in sorted(local_methods, key=len, reverse=True):
        s = re.sub(rf"(?<![.\w])\b{mname}\s*\(", f"{cls_name}.{mname}(", s)
    for cname in sorted(local_consts, key=len, reverse=True):
        s = re.sub(rf"(?<![.\w])\b{cname}\b", f"{cls_name}.{cname}", s)
    return s


def split_statements(body: str) -> list:
    """Split Java method body into structured statements (simple stmt, for, if/else)."""
    stmts = []
    i = 0
    n = len(body)
    while i < n:
        while i < n and body[i].isspace():
            i += 1
        if i >= n:
            break
        # Check `for (...) { ... }`
        m_for = re.match(r"for\s*\(", body[i:])
        if m_for:
            p_open = i + m_for.end() - 1
            # find matching )
            d = 0
            j = p_open
            while j < n:
                if body[j] == "(":
                    d += 1
                elif body[j] == ")":
                    d -= 1
                    if d == 0:
                        break
                j += 1
            header = body[p_open + 1 : j].strip()
            k = j + 1
            while k < n and body[k].isspace():
                k += 1
            if k < n and body[k] == "{":
                sub, nxt = extract_brace_block(body, k)
            else:
                semi = body.index(";", k)
                sub, nxt = body[k : semi + 1], semi + 1
            stmts.append(("for", header, split_statements(sub)))
            i = nxt
            continue

        m_if = re.match(r"if\s*\(", body[i:])
        if m_if:
            p_open = i + m_if.end() - 1
            d = 0
            j = p_open
            while j < n:
                if body[j] == "(":
                    d += 1
                elif body[j] == ")":
                    d -= 1
                    if d == 0:
                        break
                j += 1
            cond = body[p_open + 1 : j].strip()
            k = j + 1
            while k < n and body[k].isspace():
                k += 1
            if k < n and body[k] == "{":
                then_b, nxt = extract_brace_block(body, k)
            else:
                semi = body.index(";", k)
                then_b, nxt = body[k : semi + 1], semi + 1
            else_stmts = None
            k2 = nxt
            while k2 < n and body[k2].isspace():
                k2 += 1
            if body[k2:].startswith("else"):
                k3 = k2 + 4
                while k3 < n and body[k3].isspace():
                    k3 += 1
                if k3 < n and body[k3] == "{":
                    else_b, nxt = extract_brace_block(body, k3)
                    else_stmts = split_statements(else_b)
                elif body[k3:].startswith("if"):
                    # else if ...
                    else_stmts = split_statements(body[k3:])
                    nxt = n
                else:
                    semi = body.index(";", k3)
                    else_stmts = split_statements(body[k3 : semi + 1])
                    nxt = semi + 1
            stmts.append(("if", cond, split_statements(then_b), else_stmts))
            i = nxt
            continue

        # Normal `;`-terminated statement (respecting parens/braces/strings)
        d_par = 0
        d_brc = 0
        in_str = False
        j = i
        while j < n:
            ch = body[j]
            if in_str:
                if ch == "\\" and j + 1 < n:
                    j += 2
                    continue
                if ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "(":
                    d_par += 1
                elif ch == ")":
                    d_par -= 1
                elif ch == "{":
                    d_brc += 1
                elif ch == "}":
                    d_brc -= 1
                elif ch == ";" and d_par == 0 and d_brc == 0:
                    break
            j += 1
        stmt = body[i:j].strip()
        if stmt:
            stmts.append(("stmt", stmt))
        i = j + 1
    return stmts


def emit_py_stmts(stmts: list, indent: str, cls_name: str, local_methods: set, local_consts: set) -> list[str]:
    lines = []
    for item in stmts:
        kind = item[0]
        if kind == "for":
            _, header, sub = item
            # C-style for: `int i = A; i < B; i++` or foreach `Type x : iterable`
            if ":" in header and ";" not in header:
                lhs, rhs = header.split(":", 1)
                var = lhs.strip().split()[-1]
                lines.append(f"{indent}for {var} in {transpile_expr(rhs, cls_name, local_methods, local_consts)}:")
                lines.extend(emit_py_stmts(sub, indent + "    ", cls_name, local_methods, local_consts) or [f"{indent}    pass"])
            else:
                init, cond, step = [p.strip() for p in header.split(";")]
                # e.g. int i = 0
                m_init = re.match(r"(?:final\s+)?(?:int|float|double)\s+([A-Za-z0-9_]+)\s*=\s*(.+)", init)
                if m_init:
                    var, val = m_init.group(1), transpile_expr(m_init.group(2), cls_name, local_methods, local_consts)
                    lines.append(f"{indent}{var} = {val}")
                lines.append(f"{indent}while {transpile_expr(cond, cls_name, local_methods, local_consts)}:")
                lines.extend(emit_py_stmts(sub, indent + "    ", cls_name, local_methods, local_consts))
                if step.endswith("++"):
                    v = step[:-2].strip()
                    lines.append(f"{indent}    {v} += 1")
                elif step.startswith("++"):
                    v = step[2:].strip()
                    lines.append(f"{indent}    {v} += 1")
                elif step.endswith("--"):
                    v = step[:-2].strip()
                    lines.append(f"{indent}    {v} -= 1")
                elif "+=" in step:
                    v, inc = step.split("+=", 1)
                    lines.append(f"{indent}    {v.strip()} += {transpile_expr(inc, cls_name, local_methods, local_consts)}")
                else:
                    lines.append(f"{indent}    {transpile_expr(step, cls_name, local_methods, local_consts)}")
        elif kind == "if":
            _, cond, then_s, else_s = item
            lines.append(f"{indent}if {transpile_expr(cond, cls_name, local_methods, local_consts)}:")
            lines.extend(emit_py_stmts(then_s, indent + "    ", cls_name, local_methods, local_consts) or [f"{indent}    pass"])
            if else_s:
                lines.append(f"{indent}else:")
                lines.extend(emit_py_stmts(else_s, indent + "    ", cls_name, local_methods, local_consts) or [f"{indent}    pass"])
        elif kind == "stmt":
            stmt = " ".join(item[1].split())
            if stmt.startswith("return "):
                lines.append(f"{indent}return {transpile_expr(stmt[7:], cls_name, local_methods, local_consts)}")
                continue
            if stmt == "return":
                lines.append(f"{indent}return")
                continue
            m_sized = re.match(r"(?:final\s+)?[A-Za-z0-9_<>\[\].]+\[\]\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(new\s+(?:float|double|int|long)\s*\[.+\])$", stmt)
            if m_sized:
                lines.append(f"{indent}{m_sized.group(1)} = {transpile_expr(m_sized.group(2), cls_name, local_methods, local_consts)}")
                continue
            # Array literal declaration e.g. `float[][] xs = new float[][]{{...}, {...}}` or `int[] xs = {1, 2}`
            m_arr = re.match(r"(?:final\s+)?[A-Za-z0-9_<>\[\].]+\[\]\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:new\s+[A-Za-z0-9_\[\]]+\s*)?(\{.*\})$", stmt)
            if m_arr:
                var, arr_lit = m_arr.group(1), m_arr.group(2)
                arr_py = arr_lit.replace("{", "[").replace("}", "]")
                lines.append(f"{indent}{var} = {transpile_expr(arr_py, cls_name, local_methods, local_consts)}")
                continue
            # Variable declaration: `[final] TypeName<...> var = expr`
            m_decl = re.match(r"^(?:final\s+)?(?:[A-Za-z_][A-Za-z0-9_.]*(?:<[^=;]*>)?(?:\[\])*)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$", stmt)
            if m_decl:
                var, rhs = m_decl.group(1), m_decl.group(2)
                lines.append(f"{indent}{var} = {transpile_expr(rhs, cls_name, local_methods, local_consts)}")
                continue
            # Compound or simple assignment `lhs = rhs` / `lhs += rhs`
            m_asg = re.match(r"^([A-Za-z_][A-Za-z0-9_.\[\]]*)\s*([+\-*/]?=)\s*(.+)$", stmt)
            if m_asg and not stmt.startswith("if"):
                lhs, op, rhs = m_asg.group(1), m_asg.group(2), m_asg.group(3)
                lines.append(f"{indent}{lhs} {op} {transpile_expr(rhs, cls_name, local_methods, local_consts)}")
                continue
            # Expression call
            lines.append(f"{indent}{transpile_expr(stmt, cls_name, local_methods, local_consts)}")
    return lines


def build_model_env():
    PartNamesObj, part_name_consts = parse_part_names()

    class DirectionEnum:
        DOWN = "DOWN"
        UP = "UP"
        NORTH = "NORTH"
        SOUTH = "SOUTH"
        WEST = "WEST"
        EAST = "EAST"

        @staticmethod
        def values():
            return list(DIRECTIONS_ALL)

    class MthObj:
        PI = f32(math.pi)
        HALF_PI = f32(math.pi / 2.0)
        TWO_PI = f32(math.pi * 2.0)
        DEG_TO_RAD = f32(math.pi / 180.0)
        RAD_TO_DEG = f32(180.0 / math.pi)
        sin = staticmethod(lambda x: f32(math.sin(x)))
        cos = staticmethod(lambda x: f32(math.cos(x)))
        sqrt = staticmethod(lambda x: f32(math.sqrt(x)))
        abs = staticmethod(abs)
        min = staticmethod(min)
        max = staticmethod(max)
        toRadians = staticmethod(math.radians)

    def make_set(*items):
        return set(items)

    def str_concat_add(a, b):
        if isinstance(a, str) or isinstance(b, str):
            return f"{a}{b}"
        return a + b

    class EquipmentSlotObj:
        HEAD = "HEAD"
        CHEST = "CHEST"
        LEGS = "LEGS"
        FEET = "FEET"
        BODY = "BODY"
        MAINHAND = "MAINHAND"
        OFFHAND = "OFFHAND"

    class MapObj:
        @staticmethod
        def of(*kv):
            return {kv[i]: kv[i + 1] for i in range(0, len(kv), 2)}

    class MapsObj:
        @staticmethod
        def newEnumMap(d):
            return dict(d)

    class UtilObj:
        @staticmethod
        @staticmethod
        def allOfEnumExcept(enum_cls, *excluded):
            return set(DIRECTIONS_ALL) - set(excluded)

        def make(val, fn=None):
            if fn is None:
                return val()
            fn(val)
            return val

    class LegacyRandom:
        def __init__(self, seed: int):
            self.seed = (int(seed) ^ 0x5DEECE66D) & ((1 << 48) - 1)
        def nextInt(self, bound: int) -> int:
            if (bound & -bound) == bound:
                self.seed = (self.seed * 0x5DEECE66D + 0xB) & ((1 << 48) - 1)
                return ((bound * (self.seed >> 17)) >> 31)
            while True:
                self.seed = (self.seed * 0x5DEECE66D + 0xB) & ((1 << 48) - 1)
                bits = self.seed >> 17
                val = bits % bound
                if bits - val + (bound - 1) >= 0:
                    return val

    class RandomSourceObj:
        @staticmethod
        @staticmethod
        def createThreadLocalInstance(seed: int = 1870):
            return LegacyRandom(seed)

        def create(seed: int = 0):
            return LegacyRandom(seed)

    env = {
        "EquipmentSlot": EquipmentSlotObj,
        "Map": MapObj,
        "Maps": MapsObj,
        "Util": UtilObj,
        "RandomSource": RandomSourceObj,
        "CubeDeformation": CubeDeformation,
        "PartPose": PartPose,
        "CubeListBuilder": CubeListBuilder,
        "PartDefinition": PartDefinition,
        "MeshDefinition": MeshDefinition,
        "LayerDefinition": LayerDefinition,
        "MeshTransformer": MeshTransformer,
        "BabyModelTransform": BabyModelTransform,
        "ArmorModelSet": ArmorModelSet,
        "PartNames": PartNamesObj,
        "Direction": DirectionEnum,
        "DIRECTIONS_ALL": DIRECTIONS_ALL,
        "Mth": MthObj,
        "make_set": make_set,
        "math": math,
    }
    # Also expose all PartNames constants at module scope (many models do `import static ...PartNames.*` or inherit HumanoidModel constants)
    for k, v in part_name_consts.items():
        env.setdefault(k, v)

    # Scan all relevant Java files under client/model and client/renderer
    java_files = list((MC_SRC / "net/minecraft/client/model").rglob("*.java")) + [
        MC_SRC / "net/minecraft/client/renderer/blockentity/ConduitRenderer.java",
        MC_SRC / "net/minecraft/client/renderer/blockentity/DecoratedPotRenderer.java",
        MC_SRC / "net/minecraft/client/renderer/blockentity/BedRenderer.java",
        MC_SRC / "net/minecraft/client/renderer/blockentity/SignRenderer.java",
        MC_SRC / "net/minecraft/client/renderer/blockentity/HangingSignRenderer.java",
        MC_SRC / "net/minecraft/client/renderer/entity/WitherSkullRenderer.java",
    ]

    class_data = {}
    for jf in java_files:
        if not jf.is_file():
            continue
        cls_name = jf.stem
        if cls_name in (
            "ModelPart", "LayerDefinitions", "ModelLayers", "ModelLayerLocation", "PartNames",
            "CubeDeformation", "PartPose", "CubeListBuilder", "CubeDefinition",
            "PartDefinition", "MeshDefinition", "LayerDefinition", "MeshTransformer",
            "BabyModelTransform", "ArmorModelSet",
        ):
            continue
        src = strip_comments(jf.read_text())
        m_ext = re.search(rf"class\s+{cls_name}[^{{]*?\bextends\s+([A-Za-z_][A-Za-z0-9_]*)", src)
        parent = m_ext.group(1) if m_ext else None

        # Static constants (including generics with spaces & block lambdas)
        consts = {}
        for cm in re.finditer(
            r"(?:public|protected|private)?\s*static\s+final\s+([A-Za-z0-9_<>\[\]., ]+?)\s+([A-Z_][A-Z0-9_]*)\s*=\s*",
            src,
        ):
            ctype, cname = cm.group(1).strip(), cm.group(2).strip()
            if any(bad in ctype for bad in ("Identifier", "RenderType", "Codec", "ModelLayerLocation")):
                continue
            # Scan to top-level `;` respecting (), {}, ""
            j = cm.end()
            dp = db = 0
            ins = False
            while j < len(src):
                ch = src[j]
                if ins:
                    if ch == "\\" and j + 1 < len(src):
                        j += 2
                        continue
                    if ch == '"':
                        ins = False
                else:
                    if ch == '"':
                        ins = True
                    elif ch == "(":
                        dp += 1
                    elif ch == ")":
                        dp -= 1
                    elif ch == "{":
                        db += 1
                    elif ch == "}":
                        db -= 1
                    elif ch == ";" and dp == 0 and db == 0:
                        break
                j += 1
            consts[cname] = (ctype, src[cm.end():j].strip())

        # Static methods returning LayerDefinition, MeshDefinition, PartDefinition, ArmorModelSet, CubeListBuilder, MeshTransformer, String, void
        methods = {}
        for mm in re.finditer(
            r"(?:public|protected|private)?\s*static\s+(?:<[^>]+>\s+)?(LayerDefinition|MeshDefinition|PartDefinition|ArmorModelSet(?:<[^>]+>)?|CubeListBuilder|MeshTransformer|void|String|int|float)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)\s*\{",
            src,
        ):
            ret_t, mname, params_raw = mm.group(1), mm.group(2), mm.group(3).strip()
            body, _ = extract_brace_block(src, mm.end() - 1)
            params = []
            if params_raw:
                # Split params at top-level `,` outside `<...>`
                cur = []
                dg = 0
                for ch in params_raw:
                    if ch == "<": dg += 1
                    elif ch == ">": dg -= 1
                    elif ch == "," and dg == 0:
                        params.append("".join(cur).strip().split()[-1])
                        cur = []
                        continue
                    cur.append(ch)
                if "".join(cur).strip():
                    params.append("".join(cur).strip().split()[-1])
            methods[(mname, len(params))] = (mname, params, body)

        class_data[cls_name] = {"parent": parent, "consts": consts, "methods": methods}

    # Resolve inheritance order so parent constants/methods exist on subclasses
    compiled_classes = set()

    def compile_class(cls_name: str):
        if cls_name in compiled_classes or cls_name not in class_data:
            return
        compiled_classes.add(cls_name)
        info = class_data[cls_name]
        parent = info["parent"]
        if parent:
            compile_class(parent)

        cls_obj = type(cls_name, (), {})
        if parent and parent in env:
            p_obj = env[parent]
            for k in dir(p_obj):
                if not k.startswith("__"):
                    setattr(cls_obj, k, getattr(p_obj, k))
        env[cls_name] = cls_obj

        all_method_names = {m[0] for m in info["methods"]}
        all_const_names = set(info["consts"].keys())
        if parent and parent in class_data:
            p = parent
            while p and p in class_data:
                all_method_names |= {m[0] for m in class_data[p]["methods"]}
                all_const_names |= set(class_data[p]["consts"].keys())
                p = class_data[p]["parent"]

        # Group overloaded methods by name -> dispatch on len(args)
        by_name = {}
        for (mname, arity), (_, params, body) in info["methods"].items():
            by_name.setdefault(mname, []).append((arity, params, body))

        for mname, overloads in by_name.items():
            fn_map = {}
            for arity, params, body in overloads:
                # Replace `"prefix" + idx` string concat safely
                body_clean = re.sub(r'"([^"]*)"\s*\+\s*([A-Za-z0-9_()+\-*/ ]+)', lambda m: f'str_concat("{m.group(1)}", {m.group(2)})', body)
                stmts = split_statements(body_clean)
                py_lines = [f"def _impl_{mname}_{arity}({', '.join(params)}):"]
                body_lines = emit_py_stmts(stmts, "    ", cls_name, all_method_names, all_const_names)
                py_lines.extend(body_lines or ["    pass"])
                code_str = "\n".join(py_lines)
                try:
                    local_ns = {}
                    env["str_concat"] = str_concat_add
                    exec(code_str, env, local_ns)
                    fn_map[arity] = local_ns[f"_impl_{mname}_{arity}"]
                except Exception:
                    pass
            if fn_map:
                parent_fn = getattr(cls_obj, mname, None)

                def make_dispatcher(_map, _parent_fn):
                    def _dispatch(*args):
                        if len(args) in _map:
                            return _map[len(args)](*args)
                        if _parent_fn is not None:
                            return _parent_fn(*args)
                        raise TypeError(f"No overload for arity {len(args)}")

                    return _dispatch

                setattr(cls_obj, mname, staticmethod(make_dispatcher(fn_map, parent_fn)))

        # Evaluate constants after methods exist
        for cname, (ctype, cexpr) in info["consts"].items():
            if "[]" in ctype:
                cexpr_py = re.sub(r"new\s+[A-Za-z0-9_\[\]]+\s*", "", cexpr).replace("{", "[").replace("}", "]")
            else:
                cexpr_py = cexpr
            py_expr = transpile_expr(cexpr_py, cls_name, all_method_names, all_const_names)
            try:
                setattr(cls_obj, cname, eval(py_expr, env))
            except Exception:
                pass

    for cname in list(class_data.keys()):
        compile_class(cname)

    return env


def bake_all_models() -> dict:
    env = build_model_env()
    layers_map, armor_sets_map, wood_types = parse_model_layers()

    class ModelLayersObj:
        pass

    for k, v in layers_map.items():
        setattr(ModelLayersObj, k, v)
    for k, v in armor_sets_map.items():
        setattr(ModelLayersObj, k, v)
    env["ModelLayers"] = ModelLayersObj

    # Execute LayerDefinitions.createRoots() statements!
    ld_src = strip_comments((MC_SRC / "net/minecraft/client/model/geom/LayerDefinitions.java").read_text())
    # Grab static constants of LayerDefinitions
    for cm in re.finditer(r"private\s+static\s+final\s+[A-Za-z0-9_<>]+\s+([A-Z0-9_]+)\s*=\s*([^;]+);", ld_src):
        cname, cexpr = cm.group(1), cm.group(2)
        env[cname] = eval(transpile_expr(cexpr, "LayerDefinitions", set(), set()), env)

    m_roots = re.search(r"createRoots\s*\(\s*\)\s*\{", ld_src)
    roots_body, _ = extract_brace_block(ld_src, m_roots.end() - 1)
    stmts = split_statements(roots_body)

    result_layers = {}

    class ResultBuilder:
        def put(self, key, val):
            if isinstance(key, str) and isinstance(val, LayerDefinition):
                result_layers[key] = val

        def build(self):
            return result_layers

    local_vars = {"result": ResultBuilder()}
    for kind, *rest in stmts:
        if kind != "stmt":
            continue
        stmt = " ".join(rest[0].split())
        if "ImmutableMap" in stmt or "missingDefinitions" in stmt or stmt.startswith("return "):
            continue
        # Variable declaration or `result.put(...)` or `ModelLayers.X.putFrom(...)`
        m_decl = re.match(r"^(?:LayerDefinition|ArmorModelSet<LayerDefinition>|MeshTransformer)\s+([A-Za-z0-9_]+)\s*=\s*(.+)$", stmt)
        try:
            if m_decl:
                vname, rhs = m_decl.group(1), m_decl.group(2)
                py_rhs = transpile_expr(rhs, "LayerDefinitions", set(), set())
                local_vars[vname] = eval(py_rhs, env, local_vars)
                env[vname] = local_vars[vname]
            else:
                py_stmt = transpile_expr(stmt, "LayerDefinitions", set(), set())
                exec(py_stmt, env, local_vars)
        except Exception as e:
            print(f"[warn] LayerDefinitions stmt skipped: {stmt[:80]} -> {e}")

    # Also ensure standing/wall signs, hanging signs, and bed head/foot layers exist if registered via WoodType streams in LayerDefinitions!
    if "minecraft:boat#water_patch" in result_layers:
        for wt in wood_types:
            if wt != "bamboo":
                result_layers.setdefault(f"minecraft:boat/{wt}#water_patch", result_layers["minecraft:boat#water_patch"])
                result_layers.setdefault(f"minecraft:chest_boat/{wt}#water_patch", result_layers["minecraft:boat#water_patch"])
    if "BedRenderer" in env:
        if hasattr(env["BedRenderer"], "createHeadLayer"):
            result_layers.setdefault("minecraft:bed_head#main", env["BedRenderer"].createHeadLayer())
        if hasattr(env["BedRenderer"], "createFootLayer"):
            result_layers.setdefault("minecraft:bed_foot#main", env["BedRenderer"].createFootLayer())
    if "SignRenderer" in env and hasattr(env["SignRenderer"], "createSignLayer"):
        for wt in wood_types:
            result_layers.setdefault(f"minecraft:sign/standing/{wt}#main", env["SignRenderer"].createSignLayer(True))
            result_layers.setdefault(f"minecraft:sign/wall/{wt}#main", env["SignRenderer"].createSignLayer(False))
    if "HangingSignRenderer" in env and hasattr(env["HangingSignRenderer"], "createHangingSignLayer"):
        # AttachmentType: CEILING, CEILING_MIDDLE, WALL
        for wt in wood_types:
            for att_key, att_val in [("ceiling", "CEILING"), ("ceiling_middle", "CEILING_MIDDLE"), ("wall", "WALL")]:
                try:
                    result_layers.setdefault(
                        f"minecraft:hanging_sign/{wt}/{att_key}#main",
                        env["HangingSignRenderer"].createHangingSignLayer(att_val),
                    )
                except Exception:
                    pass

    baked = {}
    for key, ldef in sorted(result_layers.items()):
        try:
            baked[key] = ldef.bakeRoot()
        except Exception as e:
            print(f"[warn] bakeRoot failed for {key}: {e}")
    return baked


if __name__ == "__main__":
    anims = bake_animations()
    models = bake_all_models()
    print(f"Baked {len(anims)} animations and {len(models)} entity model layers!")
    out_dir = Path("/tmp/horizon-baked")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "animations.json").write_text(json.dumps(anims, separators=(",", ":")))
    (out_dir / "entity-models.json").write_text(json.dumps(models, separators=(",", ":")))
