let nextId = 1;
const PLAYER_WIDTH = 32;
const PLAYER_HEIGHT = 32;

export function createPlayer(x, y) {
  const id = `player_${nextId++}`;
  return {
    id,
    type: 'player',
    pos: { x, y },
    vel: { x: 0, y: 0 },
    hitbox: { x: 0, y: 0, w: PLAYER_WIDTH, h: PLAYER_HEIGHT },
    hp: 3,
    maxHp: 3,
    spriteRef: 'player_default',
    faction: 'player',
    alive: true,
    createdAt: performance.now(),
    weaponLevel: 1,
    fireCooldown: 0,
    bulletType: 'single',
    scoreValue: 0,
  };
}

export function handlePlayerInput(player, input, speed, dt) {
  let dx = 0, dy = 0;
  if (input.left) dx -= 1;
  if (input.right) dx += 1;
  if (input.up) dy -= 1;
  if (input.down) dy += 1;

  const len = Math.sqrt(dx * dx + dy * dy);
  if (len > 0) {
    dx /= len;
    dy /= len;
  }

  player.vel.x = dx * speed;
  player.vel.y = dy * speed;
}