# R-Type Prototype

A browser-based R-Type-style horizontal shooter built collaboratively by the
agora hub contributors.

## Run

Open `index.html` in any modern browser:

```
open index.html
```

Or serve locally:

```
python3 -m http.server 8080
# then open http://localhost:8080
```

## Controls

| Key         | Action       |
|-------------|-------------|
| Arrow keys / WASD | Move player |
| Space / Z   | Shoot        |
| Enter       | Start / Restart |

## Gameplay

- Defeat waves of enemies (fighters, bombers, turrets)
- Collect power-ups to increase weapon level (1→3)
- Weapon levels: single shot → spread → beam
- Survive with 3 lives across 3 escalating waves
- Score points for each enemy destroyed

## Architecture

All-browser JS (ES modules + Canvas 2D API). Zero-install.

```
src/
  schema.js      — shared entity schema & collision pair rules
  registry.js    — entity CRUD, queries, lifecycle
  collision.js   — AABB collision detection
  main.js        — game loop (fixed-timestep RAF) & wiring
  renderer.js    — canvas drawing (player, enemies, bullets, effects)
  hud.js         — score/lives/wave overlay
  screens.js     — title / game over / victory screens
  gameplay/
    index.js     — gameplay hook: init + per-frame update
    player.js    — player creation & input handling
    shooting.js  — bullet creation & cooldown system
    enemies.js   — enemy patterns & wave spawning
    scoring.js   — score tracking & localStorage high score
```

## Credits

Built by the agora hub:
- **frontend** — rendering, HUD, screens, integration, docs
- **systems** — game loop, entity registry, collision detection
- **gameplay** — player controls, enemies, weapons, scoring
- **lead** — planning, orchestration, workspace scaffolding