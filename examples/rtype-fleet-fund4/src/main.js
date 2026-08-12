import { add, getAll, clear as clearRegistry, sweepDead } from './registry.js';
import { detectCollisions } from './collision.js';
import { initGameplay, updateGameplay } from './gameplay/index.js';
import { handleCollisionResponse } from './gameplay/collision-response.js';
import { saveHighScore } from './gameplay/scoring.js';
import { init as initRenderer, clear as clearCanvas, drawEntity, drawHealthBar } from './renderer.js';
import { init as initHud, drawHUD } from './hud.js';
import { init as initScreens, drawTitle, drawGameOver, drawVictory } from './screens.js';

const canvas = document.getElementById('game');
const ctx = canvas.getContext('2d');
canvas.width = 800;
canvas.height = 600;

initRenderer(canvas);
initHud(canvas);
initScreens(canvas);

const FIXED_DT = 1 / 60;
const MAX_FRAME_DT = 0.25;

const gameState = {
  score: 0,
  lives: 3,
  wave: 1,
  weaponLevel: 1,
  screen: 'title',
};

const input = { left: false, right: false, up: false, down: false, fire: false };
let accumulated = 0;
let previousTime = 0;

function handleKey(e, pressed) {
  const key = e.key;
  if (key === 'ArrowLeft' || key === 'a') input.left = pressed;
  if (key === 'ArrowRight' || key === 'd') input.right = pressed;
  if (key === 'ArrowUp' || key === 'w') input.up = pressed;
  if (key === 'ArrowDown' || key === 's') input.down = pressed;
  if (key === ' ') input.fire = pressed;

  if (pressed && gameState.screen === 'title' && (key === 'Enter' || key === ' ')) {
    startGame();
  }
  if (pressed && (gameState.screen === 'gameover' || gameState.screen === 'victory') && (key === 'Enter' || key === ' ')) {
    startGame();
  }
  if (key === ' ') e.preventDefault();
}

document.addEventListener('keydown', e => handleKey(e, true));
document.addEventListener('keyup', e => handleKey(e, false));

function startGame() {
  clearRegistry();
  const { player, state } = initGameplay();
  add(player);
  gameState.score = 0;
  gameState.lives = 3;
  gameState.wave = 1;
  gameState.screen = 'playing';
  accumulated = 0;
  window._gameplayState = state;
  window._gameplayState.wave = 1;
}

function updatePlaying(dt) {
  const entities = getAll();
  const gameplay = updateGameplay(dt, entities, input, window._gameplayState || { wave: 1 });
  for (const b of gameplay.newBullets) add(b);
  for (const e of gameplay.newEnemies) add(e);

  for (const e of entities) {
    if (e.alive && e.vel) {
      e.pos.x += e.vel.x * dt;
      e.pos.y += e.vel.y * dt;
    }
  }

  for (const e of entities) {
    if (e.alive && e.type === 'bullet' && (e.pos.x > 820 || e.pos.x < -20 || e.pos.y > 620 || e.pos.y < -20)) {
      e.alive = false;
    }
  }

  const collisions = detectCollisions();
  if (collisions.length > 0) {
    const response = handleCollisionResponse(collisions, getAll(), gameState.wave);
    gameState.score += response.scoreGained;
    for (const pu of (response.newPowerups || [])) add(pu);

    if (response.playerHit > 0) {
      gameState.lives -= response.playerHit;
      if (gameState.lives <= 0) {
        saveHighScore(gameState.score);
        gameState.screen = 'gameover';
      } else {
        const p = entities.find(e => e.type === 'player' && e.alive);
        if (p) {
          p.pos.x = 100;
          p.pos.y = 300;
          p.hp = p.maxHp;
        }
      }
    }

    const p = entities.find(e => e.type === 'player' && e.alive);
    if (p) {
      gameState.weaponLevel = p.weaponLevel;
    }
  }

  const aliveEnemies = entities.filter(e => e.type === 'enemy' && e.alive);
  gameState.wave = window._gameplayState?.wave || gameState.wave;
  if (aliveEnemies.length === 0 && gameState.wave > 3) {
    saveHighScore(gameState.score);
    gameState.screen = 'victory';
  }

  sweepDead();
}

function render() {
  clearCanvas();
  if (gameState.screen === 'title') {
    drawTitle();
  } else if (gameState.screen === 'gameover') {
    drawGameOver(gameState.score);
  } else if (gameState.screen === 'victory') {
    drawVictory(gameState.score);
  } else if (gameState.screen === 'playing') {
    const entities = getAll();
    entities.sort((a, b) => (a.zIndex || 10) - (b.zIndex || 10));
    for (const e of entities) {
      if (!e.alive) continue;
      drawEntity(e);
      if (e.type === 'player' || (e.type === 'enemy' && e.maxHp > 1)) {
        drawHealthBar(e, e.type === 'player' ? 10 : 6);
      }
    }
    drawHUD(gameState);
  }
}

function gameLoop(timestamp) {
  if (gameState.screen === 'playing') {
    const frameTime = previousTime === 0 ? FIXED_DT : Math.min((timestamp - previousTime) / 1000, MAX_FRAME_DT);
    previousTime = timestamp;
    accumulated += frameTime;
    while (accumulated >= FIXED_DT) {
      updatePlaying(FIXED_DT);
      accumulated -= FIXED_DT;
    }
  } else {
    previousTime = 0;
    accumulated = 0;
  }
  render();
  requestAnimationFrame(gameLoop);
}

requestAnimationFrame(gameLoop);