export const ENEMY_PATTERNS = {
  fighter: {
    hp: 1,
    speed: 150,
    movePattern: 'straight',
    shootInterval: 0,
    scoreValue: 100,
    spriteRef: 'enemy_fighter',
  },
  bomber: {
    hp: 3,
    speed: 80,
    movePattern: 'sine',
    shootInterval: 1.5,
    scoreValue: 300,
    spriteRef: 'enemy_bomber',
  },
  turret: {
    hp: 5,
    speed: 0,
    movePattern: 'stationary',
    shootInterval: 2.0,
    scoreValue: 500,
    spriteRef: 'enemy_turret',
  },
};

let nextEnemyId = 1;

function createEnemy(x, y, pattern, screenTop) {
  return {
    id: `enemy_${nextEnemyId++}`,
    type: 'enemy',
    faction: 'enemy',
    pos: { x, y },
    vel: { x: -pattern.speed, y: 0 },
    hitbox: { x: 0, y: 0, w: 28, h: 28 },
    hp: pattern.hp,
    maxHp: pattern.hp,
    spriteRef: pattern.spriteRef,
    faction: 'enemy',
    alive: true,
    createdAt: performance.now(),
    weaponLevel: 1,
    fireCooldown: pattern.shootInterval,
    bulletType: 'enemy',
    scoreValue: pattern.scoreValue,
    movePattern: pattern.movePattern,
    shootTimer: pattern.shootInterval,
    startY: y,
    screenTop,
  };
}

const WAVE_CONFIGS = {
  1: [
    { pattern: 'fighter', count: 6, interval: 0.4, rowY: 40 },
    { pattern: 'fighter', count: 4, interval: 0.3, rowY: 80 },
  ],
  2: [
    { pattern: 'bomber', count: 3, interval: 0.6, rowY: 50 },
    { pattern: 'fighter', count: 4, interval: 0.3, rowY: 100 },
  ],
  3: [
    { pattern: 'turret', count: 2, interval: 1.0, rowY: 60 },
    { pattern: 'bomber', count: 4, interval: 0.5, rowY: 120 },
    { pattern: 'fighter', count: 6, interval: 0.3, rowY: 160 },
  ],
};

export function spawnWave(dt, entities, gameState, patterns) {
  const newEnemies = [];
  let timer = gameState.spawnTimer;
  let wave = gameState.wave;

  timer -= dt;
  if (timer > 0) return { enemies: [], nextWave: wave, timer };

  const config = WAVE_CONFIGS[wave];
  if (!config) return { enemies: [], nextWave: wave, timer };

  if (!gameState._waveIndex) gameState._waveIndex = 0;
  let idx = gameState._waveIndex;

  if (idx < config.length) {
    const row = config[idx];
    if (!gameState._spawnCount) gameState._spawnCount = 0;
    let count = gameState._spawnCount || 0;

    const x = 800 + count * 40;
    const y = row.rowY;
    const pattern = patterns[row.pattern];
    newEnemies.push(createEnemy(x, y, pattern, 0));

    count++;
    if (count >= row.count) {
      count = 0;
      idx++;
    }
    gameState._spawnCount = count;
    gameState._waveIndex = idx;
    timer = count === 0 ? row.interval : 0.05;
  } else {
    gameState._waveIndex = 0;
    gameState._spawnCount = 0;
    wave++;
    timer = 3.0;
  }

  return { enemies: newEnemies, nextWave: wave, timer };
}