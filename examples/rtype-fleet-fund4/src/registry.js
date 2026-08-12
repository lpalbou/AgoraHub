let nextId = 0;
const entities = new Map();

export function createId() {
  return `e${nextId++}`;
}

export function add(entity) {
  entities.set(entity.id, entity);
}

export function remove(id) {
  entities.delete(id);
}

export function get(id) {
  return entities.get(id);
}

export function getAll() {
  return Array.from(entities.values());
}

export function query(fn) {
  return Array.from(entities.values()).filter(fn);
}

export function queryByType(type) {
  return query(e => e.type === type && e.alive);
}

export function queryByFaction(faction) {
  return query(e => e.faction === faction && e.alive);
}

export function collectCollidables() {
  const alive = getAll().filter(e => e.alive);
  return alive;
}

export function sweepDead() {
  for (const [id, e] of entities) {
    if (!e.alive) entities.delete(id);
  }
}

export function clear() {
  entities.clear();
}