import { COLLISION_PAIRS } from './schema.js';
import { collectCollidables } from './registry.js';

/**
 * Check AABB overlap between two entities.
 */
function overlaps(a, b) {
  const ax = a.pos.x + a.hitbox.x;
  const ay = a.pos.y + a.hitbox.y;
  const bx = b.pos.x + b.hitbox.x;
  const by = b.pos.y + b.hitbox.y;
  return (
    ax < bx + b.hitbox.w &&
    ax + a.hitbox.w > bx &&
    ay < by + b.hitbox.h &&
    ay + a.hitbox.h > by
  );
}

function shouldCollide(a, b) {
  const key = `${a.faction}-${b.faction}`;
  return COLLISION_PAIRS.some(
    ([fa, fb]) => (a.faction === fa && b.faction === fb) || (a.faction === fb && b.faction === fa)
  );
}

/**
 * @typedef {Object} CollisionEvent
 * @property {string} a
 * @property {string} b
 */

export function detectCollisions() {
  const entities = collectCollidables();
  const collisions = [];

  for (let i = 0; i < entities.length; i++) {
    for (let j = i + 1; j < entities.length; j++) {
      const a = entities[i];
      const b = entities[j];
      if (shouldCollide(a, b) && overlaps(a, b)) {
        collisions.push({ a: a.id, b: b.id });
      }
    }
  }

  return collisions;
}