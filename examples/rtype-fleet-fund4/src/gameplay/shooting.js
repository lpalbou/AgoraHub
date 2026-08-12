export const FIRE_COOLDOWN_DEFAULT = 0.25;
const BULLET_SPEED = 500;
const BULLET_WIDTH = 6;
const BULLET_HEIGHT = 4;

let nextBulletId = 1;

export function handleShooting(player, dt) {
  if (player.fireCooldown > 0) {
    player.fireCooldown -= dt;
    return null;
  }

  player.fireCooldown = FIRE_COOLDOWN_DEFAULT / player.weaponLevel;

  const bullet = {
    id: `bullet_${nextBulletId++}`,
    type: 'bullet',
    faction: 'player',
    pos: { x: player.pos.x + 32, y: player.pos.y + 12 },
    vel: { x: BULLET_SPEED, y: 0 },
    hitbox: { x: 0, y: 0, w: BULLET_WIDTH, h: BULLET_HEIGHT },
    hp: 1,
    maxHp: 1,
    spriteRef: 'bullet1',
    faction: 'player',
    alive: true,
    createdAt: performance.now(),
    weaponLevel: player.weaponLevel,
    fireCooldown: 0,
    bulletType: player.bulletType,
    scoreValue: 0,
    damage: player.weaponLevel,
  };

  return bullet;
}

export function spawnEnemyBullet(x, y, targetX) {
  const dx = targetX - x;
  const len = Math.sqrt(dx * dx);
  const normalizedDx = len > 0 ? dx / len : 0;
  const ENEMY_BULLET_SPEED = 250;

  return {
    id: `ebullet_${nextBulletId++}`,
    type: 'bullet',
    faction: 'enemy',
    pos: { x, y: y + 16 },
    vel: { x: normalizedDx * ENEMY_BULLET_SPEED, y: 0 },
    hitbox: { x: 0, y: 0, w: 4, h: 4 },
    hp: 1,
    maxHp: 1,
    spriteRef: 'ebullet',
    faction: 'enemy',
    alive: true,
    createdAt: performance.now(),
    weaponLevel: 1,
    fireCooldown: 0,
    bulletType: 'enemy',
    scoreValue: 0,
    damage: 1,
  };
}