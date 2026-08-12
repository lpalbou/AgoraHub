let nextPowerupId = 1;

function trySpawnPowerup(enemy) {
  if (Math.random() > 0.2) return null;
  return {
    id: `powerup_${nextPowerupId++}`,
    type: 'powerup',
    faction: 'neutral',
    pos: { x: enemy.pos.x, y: enemy.pos.y },
    vel: { x: -40, y: 0 },
    hitbox: { x: 0, y: 0, w: 16, h: 16 },
    hp: 1,
    maxHp: 1,
    spriteRef: 'powerup_weapon',
    alive: true,
    createdAt: performance.now(),
    weaponLevel: 1,
    fireCooldown: 0,
    bulletType: '',
    scoreValue: 0,
    zIndex: 8,
  };
}

export function handleCollisionResponse(collisions, allEntities, currentWave) {
  let scoreGained = 0;
  let playerHit = 0;
  let playerWeaponLevel = 1;
  let victory = false;
  const newPowerups = [];

  const entityMap = {};
  for (const e of allEntities) entityMap[e.id] = e;

  for (const col of collisions) {
    const a = entityMap[col.a];
    const b = entityMap[col.b];
    if (!a || !b || !a.alive || !b.alive) continue;

    const bullet = a.type === 'bullet' ? a : b.type === 'bullet' ? b : null;
    const enemy = a.type === 'enemy' ? a : b.type === 'enemy' ? b : null;
    const hitPlayer = a.type === 'player' ? a : b.type === 'player' ? b : null;
    const powerup = a.type === 'powerup' ? a : b.type === 'powerup' ? b : null;

    if (bullet && enemy && bullet.faction !== enemy.faction) {
      const damage = bullet.damage || 1;
      enemy.hp -= damage;
      bullet.alive = false;
      if (enemy.hp <= 0) {
        enemy.alive = false;
        scoreGained += enemy.scoreValue || 0;
        const pu = trySpawnPowerup(enemy);
        if (pu) newPowerups.push(pu);
      }
    }

    if (hitPlayer && enemy) {
      hitPlayer.hp -= 1;
      enemy.alive = false;
      if (hitPlayer.hp <= 0) {
        hitPlayer.hp = hitPlayer.maxHp;
        hitPlayer.alive = true;
        playerHit += 1;
      }
    }

    if (hitPlayer && bullet && bullet.faction === 'enemy') {
      hitPlayer.hp -= 1;
      bullet.alive = false;
      if (hitPlayer.hp <= 0) {
        hitPlayer.hp = hitPlayer.maxHp;
        hitPlayer.alive = true;
        playerHit += 1;
      }
    }

    if (hitPlayer && powerup) {
      powerup.alive = false;
      hitPlayer.weaponLevel = Math.min(3, (hitPlayer.weaponLevel || 1) + 1);
      hitPlayer.bulletType = hitPlayer.weaponLevel >= 3 ? 'beam' : hitPlayer.weaponLevel >= 2 ? 'spread' : 'single';
    }
  }

  const player = allEntities.find(e => e.type === 'player' && e.alive);
  if (player) {
    playerWeaponLevel = player.weaponLevel;
  }

  const aliveEnemies = allEntities.filter(e => e.type === 'enemy' && e.alive);
  if (aliveEnemies.length === 0 && currentWave >= 4) {
    victory = true;
  }

  return { scoreGained, playerHit, playerWeaponLevel, victory, wave: 0, newPowerups };
}