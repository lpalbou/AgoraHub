import { handlePlayerInput, createPlayer } from './player.js';
import { handleShooting, spawnEnemyBullet } from './shooting.js';
import { spawnWave, ENEMY_PATTERNS } from './enemies.js';

const PLAYER_SPEED = 300;
const PLAYER_START_X = 100;
const PLAYER_START_Y = 300;

export function initGameplay() {
  const player = createPlayer(PLAYER_START_X, PLAYER_START_Y);
  player.faction = 'player';
  const state = {
    playerId: player.id,
    wave: 1,
    enemiesRemaining: 0,
    spawnTimer: 2.0,
    spawnInterval: 2.0,
  };
  return { player, state };
}

function enemyShooting(dt, entities, player) {
  const newBullets = [];
  for (const e of entities) {
    if (e.type !== 'enemy' || !e.alive) continue;
    if (e.hp <= 0) continue;
    if (!e.fireCooldown || e.fireCooldown <= 0) continue;

    e.shootTimer = (e.shootTimer ?? e.fireCooldown) - dt;
    if (e.shootTimer > 0) continue;

    e.shootTimer = e.fireCooldown;
    const targetX = player ? player.pos.x : 400;
    const bullet = spawnEnemyBullet(e.pos.x, e.pos.y, targetX);
    bullet.faction = 'enemy';
    newBullets.push(bullet);
  }
  return newBullets;
}

function updateEnemyMovement(dt, entities) {
  for (const e of entities) {
    if (e.type !== 'enemy' || !e.alive) continue;
    if (e.movePattern === 'sine') {
      e._sineTime = (e._sineTime || 0) + dt;
      e.vel.y = Math.sin(e._sineTime * 3) * 100;
    }
  }
}

export function updateGameplay(dt, entities, input, gameState) {
  const player = entities.find(e => e.type === 'player' && e.alive);
  if (!player) return { newBullets: [], newEnemies: [] };

  handlePlayerInput(player, input, PLAYER_SPEED, dt);
  player.pos.x = Math.max(0, Math.min(800 - 32, player.pos.x));
  player.pos.y = Math.max(0, Math.min(600 - 32, player.pos.y));

  updateEnemyMovement(dt, entities);

  const bullets = [];
  if (input.fire) {
    const shot = handleShooting(player, dt);
    if (shot) bullets.push(shot);
  }

  let newEnemies = [];
  if (gameState.wave > 0) {
    const waveResult = spawnWave(dt, entities, gameState, ENEMY_PATTERNS);
    newEnemies = waveResult.enemies;
    gameState.wave = waveResult.nextWave;
    gameState.spawnTimer = waveResult.timer;
    gameState.enemiesRemaining += newEnemies.length;
  }

  const enemyBullets = enemyShooting(dt, entities, player);

  return { newBullets: [...bullets, ...enemyBullets], newEnemies };
}