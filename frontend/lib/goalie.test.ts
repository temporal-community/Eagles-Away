import { describe, expect, it } from "vitest";
import { GOAL_LEFT, GOAL_RIGHT, KEEPER_SPEED, REACH, type GoalieState, aim, ballAt, initialGoalie, start, step } from "./goalie";

// Plays until the next shot resolves, holding Ziggy wherever `keeper` says once the ball is kicked.
function playShot(state: GoalieState, keeper: (target: number) => number, random: () => number) {
  for (let i = 0; i < 1000; i++) {
    if (state.shot) state = aim(state, keeper(state.shot.toX));
    const result = step(state, 1 / 60, random);
    state = result.state;
    if (result.outcome) return { state, outcome: result.outcome };
  }
  throw new Error("shot never resolved");
}

const seq = (...values: number[]) => { let i = 0; return () => values[i++ % values.length]; };

describe("goalie game", () => {
  it("does nothing until the player starts", () => {
    const idle = initialGoalie();
    expect(step(idle, 1).state).toBe(idle);
  });

  it("counts a save when Ziggy reaches the ball and a goal when he doesn't", () => {
    const random = seq(0.9, 0.5, 0.5);
    const saved = playShot(start(initialGoalie()), x => x, random);
    expect(saved.outcome).toBe("save");
    expect(saved.state).toMatchObject({ saves: 1, goals: 0, streak: 1, best: 1 });

    const conceded = playShot(saved.state, x => (x > 0.5 ? GOAL_LEFT : GOAL_RIGHT), random);
    expect(conceded.outcome).toBe("goal");
    expect(conceded.state).toMatchObject({ saves: 1, goals: 1, streak: 0, best: 1 });
  });

  it("judges the save where the ball crosses the line, within REACH", () => {
    const random = seq(0.5, 0.5, 0.5);
    const edge = playShot(start(initialGoalie()), x => x + REACH * 0.9, random);
    expect(edge.outcome).toBe("save");
    const miss = playShot(start(initialGoalie()), x => x + REACH * 1.6, random);
    expect(miss.outcome).toBe("goal");
  });

  it("speeds shots up after each save and eases off after a goal", () => {
    const random = seq(0.3, 0.5, 0.5);
    let state = start(initialGoalie());
    const first = state.flight;
    state = playShot(state, x => x, random).state;
    expect(state.flight).toBeLessThan(first);
    const quicker = state.flight;
    state = playShot(state, () => GOAL_RIGHT, seq(0.05, 0.5, 0.5)).state;
    expect(state.flight).toBeGreaterThan(quicker);
  });

  it("limits how fast Ziggy can dive and keeps him inside the posts", () => {
    let state = aim(start(initialGoalie()), 5);
    expect(state.targetX).toBe(GOAL_RIGHT);
    state = step(state, 0.1).state;
    expect(state.keeperX).toBeCloseTo(0.5 + KEEPER_SPEED * 0.1);
  });

  it("curves the ball mid-flight but lands it on the chosen target", () => {
    const shot = { fromX: 0.5, toX: 0.7, curve: 0.1, flight: 1, progress: 0.5 };
    expect(ballAt(shot).x).toBeCloseTo(0.7);
    expect(ballAt({ ...shot, progress: 1 }).x).toBeCloseTo(0.7);
    expect(ballAt({ ...shot, curve: 0, progress: 0.5 }).x).toBeCloseTo(0.6);
  });
});
