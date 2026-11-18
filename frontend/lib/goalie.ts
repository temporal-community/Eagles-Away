// Rules for the "Ziggy in goal" mini-game shown while a trip is being planned.
// Pure and frame-rate independent: the canvas component only feeds in time and pointer
// position and draws whatever state comes back. Coordinates are fractions of the pitch:
// x 0..1 left to right, y 0 at the penalty spot down to GOAL_LINE where Ziggy stands.

export const SPOT_Y = 0.1;
export const GOAL_LINE = 0.86;
export const GOAL_LEFT = 0.16;
export const GOAL_RIGHT = 0.84;
/** Default half-width Ziggy covers, ball radius included; the canvas sets the real one from the sprite. */
export const REACH = 0.085;
/** Fastest Ziggy can move, in pitch widths per second, so a save is a real dive. */
export const KEEPER_SPEED = 1.5;
const FIRST_FLIGHT = 1.5;
const FASTEST_FLIGHT = 0.62;
const SPEED_UP = 0.94;
const RESULT_PAUSE = 0.75;
const WIND_UP = 0.45;

export type Outcome = "save" | "goal";

export type Shot = { fromX: number; toX: number; curve: number; flight: number; progress: number };

export type GoalieState = {
  phase: "idle" | "playing";
  keeperX: number;
  targetX: number;
  shot: Shot | null;
  /** Seconds left showing the last outcome, then winding up the next kick. */
  pause: number;
  last: Outcome | null;
  saves: number;
  goals: number;
  streak: number;
  best: number;
  flight: number;
  /** Half-width of the save zone as a pitch fraction, so saves match what's drawn. */
  reach: number;
};

export function initialGoalie(): GoalieState {
  return { phase: "idle", keeperX: 0.5, targetX: 0.5, shot: null, pause: 0, last: null, saves: 0, goals: 0, streak: 0, best: 0, flight: FIRST_FLIGHT, reach: REACH };
}

export function start(state: GoalieState): GoalieState {
  return { ...initialGoalie(), phase: "playing", best: state.best, keeperX: state.keeperX, targetX: state.targetX, reach: state.reach, pause: WIND_UP };
}

/** Pointer position as a pitch fraction; Ziggy walks towards it at KEEPER_SPEED. */
export function aim(state: GoalieState, x: number): GoalieState {
  return { ...state, targetX: clamp(x, GOAL_LEFT, GOAL_RIGHT) };
}

export function kick(flight: number, random: () => number): Shot {
  const toX = GOAL_LEFT + 0.04 + random() * (GOAL_RIGHT - GOAL_LEFT - 0.08);
  // Bend late shots away from the middle so watching the start of the flight isn't enough.
  const curve = (random() - 0.5) * 0.24;
  return { fromX: 0.5 + (random() - 0.5) * 0.12, toX, curve, flight, progress: 0 };
}

/** Ball position along its curved flight, plus a 0..1 depth used to grow it as it nears goal. */
export function ballAt(shot: Shot): { x: number; y: number; depth: number } {
  const t = clamp(shot.progress, 0, 1);
  const x = shot.fromX + (shot.toX - shot.fromX) * t + shot.curve * Math.sin(Math.PI * t);
  return { x, y: SPOT_Y + (GOAL_LINE - SPOT_Y) * t, depth: t };
}

export function step(state: GoalieState, dt: number, random: () => number = Math.random): { state: GoalieState; outcome?: Outcome } {
  if (state.phase !== "playing") return { state };
  const move = clamp(state.targetX - state.keeperX, -KEEPER_SPEED * dt, KEEPER_SPEED * dt);
  let next: GoalieState = { ...state, keeperX: state.keeperX + move };
  if (!next.shot) {
    const pause = next.pause - dt;
    return { state: pause > 0 ? { ...next, pause } : { ...next, pause: 0, shot: kick(next.flight, random) } };
  }
  const shot = { ...next.shot, progress: next.shot.progress + dt / next.shot.flight };
  if (shot.progress < 1) return { state: { ...next, shot } };
  const saved = Math.abs(ballAt(shot).x - next.keeperX) <= next.reach;
  const streak = saved ? next.streak + 1 : 0;
  next = {
    ...next,
    shot: null,
    pause: RESULT_PAUSE + WIND_UP,
    last: saved ? "save" : "goal",
    saves: next.saves + (saved ? 1 : 0),
    goals: next.goals + (saved ? 0 : 1),
    streak,
    best: Math.max(next.best, streak),
    // Every save makes the next shot quicker; conceding eases off a little.
    flight: saved ? Math.max(FASTEST_FLIGHT, next.flight * SPEED_UP) : Math.min(FIRST_FLIGHT, next.flight / SPEED_UP),
  };
  return { state: next, outcome: next.last! };
}

const clamp = (n: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, n));
