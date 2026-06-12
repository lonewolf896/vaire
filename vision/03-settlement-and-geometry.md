# 03 — Settlement & Geometry

## The lattice: hexagons (Central Place Theory)

The settlement is a **hexagonal lattice** — the form Walter Christaller (1933) showed is
optimal for distributing settlements across a plane. Reasons it is genuinely right, not
just elegant:

- **Honeycomb optimality** — hexagons tile with the least perimeter per area (honeycomb
  conjecture), so minimum shared boundary = minimum infrastructure.
- **Uniform adjacency** — every hex has **6 equidistant neighbors** (vs. a square grid's
  4-orthogonal + 4-diagonal at two distances). Equal access in all directions; no
  privileged direction; a lattice has **no center to capture** (anti-Pullman, pro-
  equilibrium).

## Feature placement — geometry does the work

| Location | Shared by | Use |
|---|---|---|
| **Interior** | 1 town | Walkable core — housing, local co-ops, at-cost grocery, transit stop |
| **Edge** | exactly **2 towns** | Truck/freight service network **+ shared utility runs** (one trench feeds two hexes) |
| **Vertex** (3 hexes meet) | exactly **3 towns** | Large shared infrastructure — hospital, university, modular factory, substation, food processing |

Consequences that fall out for free:
- **Walkability vs. trucks solves itself:** people in the interior, goods on the shared
  edges. "Front for people, back for goods" becomes "interior for people, edges for trucks,"
  and one service road serves two towns.
- **Cheap routing (see below):** shared edges carry every linear utility for both adjacent
  hexes off one corridor.
- **Governance emerges from geometry:** a vertex feature serving 3 towns is *jointly
  governed by 3 town-unions* — a captured town can't seize shared infrastructure two
  neighbors co-own. The honeycomb forces federation and checks-and-balances. (See `06`.)

## Routing: the lattice *is* the utility network

The hex edges are not just service roads — they are a **shared utility spine**, and this is
one of the geometry's biggest cost wins. Every linear network runs the same edges:

| Network | Routed on | Why the hex helps |
|---|---|---|
| **Water** (potable + greenhouse makeup) | Edges, off the coastal trunk | One main feeds two hexes |
| **Sewage / greywater** | Edges, flowing toward the downstream farm hexes | Gravity-fed reuse loop (town → treatment → farm) |
| **Transport** (freight) | Edges; passenger rail through centers | Two networks, cleanly separated |
| **Data** (fiber/conduit) | Edges, in the same trench | Marginal cost ≈ a duct in an already-open trench |
| **Power** | Edges + vertex substations | Shared distribution |

Because honeycomb tiling has the **least shared boundary per unit area** (honeycomb
conjecture), this is the *minimum* possible trenching to connect a given population — every
edge is dug once and serves **two** towns, and every vertex serves **three**. Dig the
corridor once, lay water + sewer + power + fiber together, and you've provisioned two hexes.
Co-trenching also means one maintenance access path for all utilities, reinforcing the
standardized-fleet maintenance logic (`04`). The edges carry the *hard* shared
infrastructure; the interiors stay *soft* and walkable.

## Movement: light rail through centers, trucks on edges

- **Passenger light rail runs through hex *centers*; freight trucks run the *edges*.** Two
  networks, cleanly separated by geometry — they never fight.
- **Intra-town = walk** (15-minute city); **inter-town = ride.** One mode per scale.
- Stations land on the densest walkable core (textbook transit-oriented development, by
  construction). At ~1.5–2 km hex pitch, adjacent centers are ~1.5–2 km apart — ideal
  light-rail stop spacing.
- **Use *light* rail deliberately:** it can run in-street at pedestrian speed through the
  core (crossable, permeable), then accelerate between towns — avoiding the "wrong side of
  the tracks" severance a fast line would cause.

## Size: true 15-minute cities

Hex pitch ~**1.5–2 km** (walk anywhere in ~15 min). Making hexes much bigger destroys the
walkable premise. If tiers are ever needed (town → regional center), use Christaller's
k=3/4/7 nesting — but resist hierarchy as long as possible (flat = harder to capture).

## Farm hexes (required)

Farm hexes are **controlled-environment agriculture** (see `05`), not open field. They
**ring the town hexes and sit hydrologically downstream** (town wastewater → treated →
farm irrigation makeup). Produce moves one hex-hop on the shared freight edges to the
town-center grocery; processing/aquaculture sits at shared vertices.

## The coastal asymmetry — the one thing geometry can't fix

Water and magnesium enter from **one fixed point** (the Sea of Cortez desal intake), not a
convenient lattice vertex. So the system is **not isotropic**: there is a trunk (rail +
water + freight) running inland from the coast, with the lattice hung off it
(Christaller's transport principle, k=4). Two hard consequences:

1. **Terrain deforms the ideal.** Mountains, land titles, and the pipeline route bend the
   lattice. The hexagon is the *organizing ideal you relax against topography*, not a rigid
   stamp.
2. **The coastal water node is the single capture point.** Geometry distributes power
   across the lattice but **cannot distribute a point resource** — whoever controls the
   desal intake controls the water downstream. It therefore gets the *strongest*
   constitutional protection (see `06`): deepest trust, widest coalition, most binding
   pre-commitments.

## Cap-and-spawn

Growth = **stamp the next hex shell**, don't enlarge existing towns. Each new hex is a
standardized hard shell (geometry + charter + infra spec + capital rules + transfer
triggers) filled with **unique soft content**. *Standardize the shell, vary the fill.*
Cap-and-spawn keeps towns small enough for the union to actually govern, and divergent
enough to have real character (which draws people — agglomeration — and resists capture).
**Not clone-and-paste:** sameness of *structure* must enable diversity of *content*.
