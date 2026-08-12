/**
 * Entity schema — single source of truth for the shared data contract.
 * All seats must agree before implementation begins.
 *
 * An entity is a plain object with a type tag and lifecycle properties.
 * Systems owns the entity registry; gameplay and frontend read/mutate it.
 */

/** @typedef {'player'|'enemy'|'bullet'|'powerup'|'explosion'} EntityType */

/** @typedef {'player'|'enemy'|'neutral'} Faction */

/**
 * @typedef {Object} Vec2
 * @property {number} x
 * @property {number} y
 */

/**
 * @typedef {Object} Hitbox
 * @property {number} x - offset from entity position
 * @property {number} y
 * @property {number} w
 * @property {number} h
 */

/**
 * @typedef {Object} Entity
 * @property {string} id - unique per entity
 * @property {EntityType} type
 * @property {Faction} faction
 * @property {Vec2} pos
 * @property {Vec2} vel
 * @property {Hitbox} hitbox
 * @property {number} hp - current health
 * @property {number} maxHp
 * @property {string} spriteRef - key into frontend's sprite atlas
 * @property {number} createdAt - timestamp
 * @property {boolean} alive
 * @property {number} facing - -1 (left) or 1 (right); frontend mirrors sprite
 * @property {number} animFrame - current animation frame index
 * @property {number} zIndex - render order (0=bg, 10=entities, 20=effects)
 * @property {number} weaponLevel - player's current weapon power (1-3)
 * @property {number} fireCooldown - frames remaining before next shot
 * @property {string} bulletType - 'single' | 'spread' | 'beam'
 * @property {number} scoreValue - points awarded when killed
 */

/**
 * Renderable state — what frontend reads each frame.
 * @typedef {Object} RenderState
 * @property {number} score
 * @property {number} lives
 * @property {number} wave
 * @property {number} weaponLevel
 * @property {'title'|'playing'|'gameover'|'victory'} screen
 */

// Collision pair rules: [factionA, factionB] pairs that should collide.
// Player faction covers player body AND player bullets.
// Enemy faction covers enemy body AND enemy bullets.
export const COLLISION_PAIRS = [
  ['player', 'enemy'],
  ['player', 'powerup'],
];

export const SCHEMA_VERSION = '0.3.0';