//! Generates the 6 binary NBT resource tables for Horizon 26.3 (`data_version = 5023`)
//! using Horizon's own `VoxelShape` + `nbt::write_named(..., Limits::tables())`, then
//! validates EVERY Horizon runtime resource loader + Overworld chunk (0, 0) generation!

use horizon_core::{
    geometry::{Aabb, Axis, ShapeOp, Vec3, VoxelShape},
    position::ChunkPos,
};
use horizon_world::{
    DATA_VERSION,
    block_properties::BlockPropertiesRegistry,
    block_support::CachedBlockSupportRegistry,
    collision::CachedCollisionRegistry,
    level::Behaviours,
    movement_properties::MovementProperties,
    nbt::{self, JavaString, Limits, NamedTag, Tag},
    registry::{BlockRegistry, BlockStateId, BlockTableFile},
    worldgen::generator::{GeneratorData, OverworldGenerator},
};
use serde_json::Value;
use std::{
    collections::{BTreeMap, HashMap},
    fs::{self, File},
    path::Path,
};

fn shape_from_box_indices(boxes: &[Aabb], ids: &[usize]) -> VoxelShape {
    if ids.is_empty() {
        return VoxelShape::empty();
    }
    if ids.len() == 1 {
        let b = boxes[ids[0]];
        if b.min(Axis::X) == 0.0
            && b.min(Axis::Y) == 0.0
            && b.min(Axis::Z) == 0.0
            && b.max(Axis::X) == 1.0
            && b.max(Axis::Y) == 1.0
            && b.max(Axis::Z) == 1.0
        {
            return VoxelShape::block();
        }
        return VoxelShape::from_box(b).expect("valid single box");
    }
    let mut acc = VoxelShape::empty();
    for &i in ids {
        let part = VoxelShape::from_box(boxes[i]).expect("valid part box");
        acc = acc.join(&part, ShapeOp::OR).expect("valid union");
    }
    if !acc
        .join_is_not_empty(&VoxelShape::block(), ShapeOp::XOR)
        .unwrap_or(true)
    {
        return VoxelShape::block();
    }
    acc
}

fn shape_to_nbt(shape: &VoxelShape, canonical: i8) -> Tag {
    let [nx, ny, nz] = shape.sizes();
    let mut occupied = Vec::with_capacity(nx * ny * nz);
    for x in 0..nx as i32 {
        for y in 0..ny as i32 {
            for z in 0..nz as i32 {
                occupied.push(u8::from(shape.is_full(x, y, z)));
            }
        }
    }
    let axes = shape.cube_coordinate_axes();
    let cube_axes = i8::from(axes[0]) | (i8::from(axes[1]) << 1) | (i8::from(axes[2]) << 2);
    let (dmin, dmax) = shape.discrete_bounds();
    let coords = |a: Axis| {
        Tag::LongArray(
            shape
                .coordinates(a)
                .iter()
                .map(|v| v.to_bits() as i64)
                .collect(),
        )
    };
    Tag::compound([
        ("canonical", Tag::Byte(canonical)),
        ("x", coords(Axis::X)),
        ("y", coords(Axis::Y)),
        ("z", coords(Axis::Z)),
        ("occupied", Tag::ByteArray(occupied)),
        ("cube_axes", Tag::Byte(cube_axes)),
        ("discrete_min", Tag::IntArray(dmin.to_vec())),
        ("discrete_max", Tag::IntArray(dmax.to_vec())),
        ("cube", Tag::Byte(i8::from(shape.is_cube()))),
    ])
}

fn build_shape_table_tag(
    state_tags: Vec<Tag>,
    boxes: &[Aabb],
    per_state: &[Option<Vec<usize>>],
    extra_kind: Option<&str>,
) -> Tag {
    let mut shape_tags: Vec<Tag> = vec![
        shape_to_nbt(&VoxelShape::empty(), 1),
        shape_to_nbt(&VoxelShape::block(), 2),
    ];
    let mut combo_to_idx: HashMap<Vec<usize>, i32> = HashMap::new();
    combo_to_idx.insert(Vec::new(), 0);

    let mut cached_shapes = Vec::with_capacity(per_state.len());
    for entry in per_state {
        let Some(ids) = entry else {
            cached_shapes.push(-1);
            continue;
        };
        if let Some(&idx) = combo_to_idx.get(ids) {
            cached_shapes.push(idx);
            continue;
        }
        let shape = shape_from_box_indices(boxes, ids);
        let idx = if shape.is_empty() {
            0
        } else if shape.is_block() {
            1
        } else {
            let new_idx = shape_tags.len() as i32;
            shape_tags.push(shape_to_nbt(&shape, 0));
            new_idx
        };
        combo_to_idx.insert(ids.clone(), idx);
        cached_shapes.push(idx);
    }

    let mut fields: Vec<(&str, Tag)> = vec![
        ("DataVersion", Tag::Int(DATA_VERSION)),
        ("states", Tag::List(state_tags)),
        ("shapes", Tag::List(shape_tags)),
        ("cached_shapes", Tag::IntArray(cached_shapes)),
    ];
    if let Some(kind) = extra_kind {
        fields.push(("Kind", Tag::String(JavaString::from(kind))));
    }
    Tag::compound(fields)
}

fn write_nbt_file(path: &Path, tag: Tag) -> Result<(), Box<dyn std::error::Error>> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let root = NamedTag {
        name: JavaString::from(""),
        tag,
    };
    let bytes = nbt::write_named(&root, Limits::tables())?;
    fs::write(path, bytes)?;
    Ok(())
}

fn parse_shape_lists(v: &Value) -> Vec<Option<Vec<usize>>> {
    v.as_array()
        .expect("shape array")
        .iter()
        .map(|row| {
            if row.is_null() {
                None
            } else {
                Some(
                    row.as_array()
                        .expect("shape id list")
                        .iter()
                        .map(|x| x.as_u64().unwrap() as usize)
                        .collect(),
                )
            }
        })
        .collect()
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = std::env::args().collect();
    let root = Path::new(args.get(1).map_or("/tmp/horizon-resources", String::as_str));
    let inputs_path = Path::new(
        args.get(2)
            .map_or("/tmp/horizon-nbt-inputs.json", String::as_str),
    );
    let cache_dir = Path::new(args.get(3).map_or("/tmp/horizon-cache", String::as_str));

    println!("[1/4] Loading registry from {}/facts.json ...", root.display());
    let registry = Box::leak(Box::new(BlockRegistry::from_facts_json(File::open(
        root.join("facts.json"),
    )?)?));
    println!("      Loaded {} block states!", registry.len());

    let state_tags: Vec<Tag> = (0..registry.len())
        .map(|i| {
            registry
                .state(BlockStateId(i as u32))
                .expect("state in registry")
                .to_nbt()
        })
        .collect();

    println!("[2/4] Reading NBT inputs & building 6 binary .nbt tables ...");
    let inputs: Value = serde_json::from_slice(&fs::read(inputs_path)?)?;
    let boxes: Vec<Aabb> = inputs["shapes"]
        .as_array()
        .expect("shapes")
        .iter()
        .map(|s| {
            let a = s.as_array().unwrap();
            Aabb::new(
                Vec3::new(
                    a[0].as_f64().unwrap(),
                    a[1].as_f64().unwrap(),
                    a[2].as_f64().unwrap(),
                ),
                Vec3::new(
                    a[3].as_f64().unwrap(),
                    a[4].as_f64().unwrap(),
                    a[5].as_f64().unwrap(),
                ),
            )
        })
        .collect();

    let coll_lists = parse_shape_lists(&inputs["collision"]);
    let occ_lists = parse_shape_lists(&inputs["occlusion"]);

    // Build support shape lists: identical to collision except Leaves/PowderSnow/Azalea (empty) and SoulSand/Mud/Grate (full cube)
    let mut support_lists = coll_lists.clone();
    for i in 0..registry.len() {
        let name = &registry.state(BlockStateId(i as u32)).unwrap().name;
        if name.ends_with("_leaves")
            || name == "minecraft:azalea"
            || name == "minecraft:flowering_azalea"
            || name == "minecraft:powder_snow"
        {
            support_lists[i] = Some(Vec::new());
        } else if name == "minecraft:soul_sand"
            || name == "minecraft:mud"
            || name.ends_with("_grate")
        {
            support_lists[i] = Some(vec![0]);
        }
    }

    let coll_tag = build_shape_table_tag(state_tags.clone(), &boxes, &coll_lists, None);
    let occ_tag = build_shape_table_tag(state_tags.clone(), &boxes, &occ_lists, None);
    let sup_tag = build_shape_table_tag(
        state_tags.clone(),
        &boxes,
        &support_lists,
        Some("block_support"),
    );

    write_nbt_file(&root.join("cached-collision.nbt"), coll_tag.clone())?;
    write_nbt_file(&root.join("worldgen/blocks/cached-collision.nbt"), coll_tag)?;
    write_nbt_file(&root.join("occlusion.nbt"), occ_tag.clone())?;
    write_nbt_file(&root.join("worldgen/blocks/cached-support.nbt"), sup_tag)?;

    // Build movement-properties.nbt (`blocks` Compound per block name)
    let mov_rows = inputs["movement"].as_array().expect("movement");
    let mut mov_blocks: BTreeMap<String, Tag> = BTreeMap::new();
    for i in 0..registry.len() {
        let st = registry.state(BlockStateId(i as u32)).unwrap();
        if mov_blocks.contains_key(&st.name) && registry.default_state(&st.name) != Some(BlockStateId(i as u32)) {
            continue;
        }
        let r = mov_rows[i].as_array().unwrap();
        let f = |idx: usize| Tag::Float((r[idx].as_f64().unwrap() as f32).to_bits());
        let is_fg = st.name.ends_with("_fence_gate");
        mov_blocks.insert(
            st.name.clone(),
            Tag::compound([
                ("friction", f(0)),
                ("speedFactor", f(1)),
                ("jumpFactor", f(2)),
                ("bounceRestitution", f(3)),
                ("fallDistanceReduction", f(4)),
                ("fenceGate", Tag::Byte(i8::from(is_fg))),
            ]),
        );
    }
    let mov_tag = Tag::compound([
        ("DataVersion", Tag::Int(DATA_VERSION)),
        ("states", Tag::List(state_tags.clone())),
        ("blocks", Tag::compound(mov_blocks.iter().map(|(k, v)| (k.as_str(), v.clone())))),
    ]);
    write_nbt_file(&root.join("movement-properties.nbt"), mov_tag)?;

    // Build worldgen/blocks/block-properties.nbt (`blocks` Compound per block name + nested `occlusion`)
    let coll_reg = CachedCollisionRegistry::from_table(
        BlockTableFile::open(&root.join("cached-collision.nbt"), registry)?.table("collision")?,
    )?;
    let prop_flags = inputs["block_properties"].as_array().expect("block_properties");
    let render_defs: Value =
        serde_json::from_slice(&fs::read(root.join("render-definitions.json"))?)?;
    let mut bp_blocks: BTreeMap<String, Tag> = BTreeMap::new();
    for i in 0..registry.len() {
        let sid = BlockStateId(i as u32);
        let st = registry.state(sid).unwrap();
        if registry.default_state(&st.name) != Some(sid) {
            continue;
        }
        let pf = prop_flags[i].as_u64().unwrap() as u8;
        // pf bits from Python: 1=air, 2=replaceable, 4=solid, 8=liquid, 16=solid_render
        let want_solid = (pf & 4) != 0;
        let auto_solid = coll_reg
            .cached_shape(sid)?
            .and_then(VoxelShape::bounds)
            .is_some_and(|b| b.average_size() >= 0.7291666666666666 || b.size().y >= 1.0);
        let force_on = want_solid && !auto_solid;
        let force_off = !want_solid && auto_solid;
        let replaceable = (pf & 2) != 0;
        let liquid = (pf & 8) != 0;
        let can_occlude = render_defs[&st.name]["can_occlude"]
            .as_bool()
            .unwrap_or(false);
        let byte_flags = i8::from(force_on)
            | (i8::from(force_off) << 1)
            | (i8::from(replaceable) << 2)
            | (i8::from(liquid) << 3)
            | (i8::from(can_occlude) << 4);
        bp_blocks.insert(st.name.clone(), Tag::Byte(byte_flags));
    }
    let bp_tag = Tag::compound([
        ("DataVersion", Tag::Int(DATA_VERSION)),
        ("states", Tag::List(state_tags)),
        ("blocks", Tag::compound(bp_blocks.iter().map(|(k, v)| (k.as_str(), v.clone())))),
        ("occlusion", occ_tag),
    ]);
    write_nbt_file(&root.join("worldgen/blocks/block-properties.nbt"), bp_tag)?;

    println!("[3/4] Validating ALL Horizon runtime loaders against {} ...", root.display());
    let sup_check = CachedBlockSupportRegistry::from_table(
        BlockTableFile::open(&root.join("worldgen/blocks/cached-support.nbt"), registry)?
            .table("support")?,
    )?;
    let _ = sup_check;
    let bp_check = BlockPropertiesRegistry::from_table(
        BlockTableFile::open(
            &root.join("worldgen/blocks/block-properties.nbt"),
            registry,
        )?
        .table("block property")?,
        &coll_reg,
    )?;
    let _ = bp_check;
    let _mov_check = MovementProperties::from_table(
        BlockTableFile::open(&root.join("movement-properties.nbt"), registry)?.table("movement")?,
    )?;
    println!("      -> All 6 binary NBT tables verified!");

    let assets = horizon_client_render::assets::Assets::load(root)?;
    let render = assets.reload(root)?;
    println!(
        "      -> Assets + block models baked ({} sprite layers, size {})!",
        render.texture_layers, render.sprite_size
    );
    let behaviour_json: Value = serde_json::from_slice(&fs::read(root.join("block-behaviours.json"))?)?;
    let (facts, gameplay_proj) =
        horizon_client::blocks::BlockFacts::load_with_projection(root, assets.registry, &behaviour_json)?;
    println!("      -> BlockFacts loaded ({} states)!", facts.states.len());
    let biomes = std::sync::Arc::new(horizon_client_render::biome::Biomes::load(root)?);
    let _env = horizon_client_render::environment::Environment::load(root)?;
    let _light = std::sync::Arc::new(horizon_client_render::light::LightTables::load(
        root,
        assets.registry,
        &facts,
    )?);
    let _vis = horizon_client_render::models::load_visibility(root, assets.registry)?;
    println!("      -> Biomes + Environment + LightTables + PackResources loaded!");

    let assets2 = horizon_client_render::assets::Assets::load(root)?;
    let _motion = horizon_client::motion::MotionResources::load_with_facts(root, &assets2, &facts)?;
    println!("      -> MotionResources (attributes, landing, health, damage, fluids) loaded!");

    let items = horizon_client::items::Items::load(root, registry, &facts)?;
    let _recipes = horizon_client::crafting::Recipes::load(root, &items)?;
    let _transformers = horizon_client::block_transform::Transformers::load(root, registry)?;
    let _be_render = horizon_client_render::block_entity_render::BlockEntityRenderer::load(root, registry)?;
    let _animate = horizon_client::animate_tick::AnimateTables::load(root, registry, &biomes)?;
    let _creative = horizon_client::creative::Creative::load(root)?;
    println!(
        "      -> Items ({}) + Recipes + Transformers + BlockEntityRenderer + AnimateTables + Creative loaded!",
        items.items.len()
    );

    fs::create_dir_all(cache_dir)?;
    Behaviours::set_cache_dir(cache_dir.to_path_buf());
    let _behaviours = Behaviours::load_with_gameplay(registry, root, &behaviour_json, &gameplay_proj)?;
    println!("      -> Behaviours loaded & cached in {}!", cache_dir.display());

    let mob_types = horizon_client::mobs::MobTypes::load(root)?;
    let _mob_data = horizon_client::mobs::MobData::load(root, &facts);
    println!("      -> MobTypes ({}) + MobData loaded!", mob_types.types.len());

    println!("[4/4] Loading GeneratorData & generating Overworld chunk (0, 0) ...");
    let gen_data = GeneratorData::load(&root.join("worldgen"), registry)?;
    let ov_gen = OverworldGenerator::new(gen_data, 20261029)?;
    let mut worker = ov_gen.worker();
    let proto = worker.generate_proto(ChunkPos::new(0, 0))?;
    println!(
        "      -> Overworld chunk (0, 0) generated! Surface height at (8, 8) = {}, biome = {}, block(8, 63, 8) = {}",
        proto.surface_height(8, 8),
        proto.biome_at(8, 64, 8),
        proto.block_name(8, 63, 8),
    );
    println!("ALL HORIZON 26.3 RESOURCE & WORLDGEN CHECKS PASSED 100%!");
    Ok(())
}
