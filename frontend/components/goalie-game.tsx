"use client";
import { useEffect, useRef } from "react";
import { GOAL_LEFT, GOAL_LINE, GOAL_RIGHT, SPOT_Y, type GoalieState, aim, ballAt, initialGoalie, start, step } from "../lib/goalie";

const BEST_KEY = "cpfc-goalie-best";
const BLUE = "#0055a5", RED = "#ee2e24";
const BALL_RADIUS = 12;

// A pointer/touch mini-game for the wait. Purely decorative for assistive tech (the
// surrounding card is aria-hidden and the live status region carries real progress).
// It never auto-plays: nothing moves until someone taps, which also respects reduced motion.
export function GoalieGame() {
  const canvas = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const el = canvas.current;
    const ctx = el?.getContext("2d");
    if (!el || !ctx) return;
    let state: GoalieState = initialGoalie();
    try { state = { ...state, best: Number(localStorage.getItem(BEST_KEY)) || 0 }; } catch { /* storage blocked */ }
    let flash = 0, frame = 0, last = 0, width = 0, height = 0;
    const ziggy = new Image();
    ziggy.src = "/ziggy.png";
    ziggy.onload = () => resize();
    const spriteHeight = () => Math.min(64, height * 0.34);
    const spriteWidth = () => spriteHeight() * (ziggy.naturalWidth ? ziggy.naturalWidth / ziggy.naturalHeight : 160 / 228);

    const resize = () => {
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      width = el.clientWidth; height = el.clientHeight;
      el.width = Math.round(width * ratio); el.height = Math.round(height * ratio);
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      // Save zone = half of Ziggy's drawn width plus the ball's radius at the goal line.
      if (width) state = { ...state, reach: (spriteWidth() / 2 + BALL_RADIUS) / width };
      draw();
    };
    const observer = new ResizeObserver(resize);
    observer.observe(el);

    const toPitch = (e: PointerEvent) => { const r = el.getBoundingClientRect(); return (e.clientX - r.left) / r.width; };
    const onMove = (e: PointerEvent) => { state = aim(state, toPitch(e)); };
    const onDown = (e: PointerEvent) => {
      state = aim(state, toPitch(e));
      if (state.phase === "idle") { state = start(state); loop(); }
    };
    el.addEventListener("pointermove", onMove);
    el.addEventListener("pointerdown", onDown);

    function loop() {
      cancelAnimationFrame(frame);
      last = performance.now();
      const tick = (now: number) => {
        // Clamp so a backgrounded tab doesn't resolve a shot in one giant step on return.
        const dt = Math.min(0.05, (now - last) / 1000);
        last = now;
        const result = step(state, dt);
        state = result.state;
        if (result.outcome) {
          flash = 0.6;
          if (state.best > 0) try { localStorage.setItem(BEST_KEY, String(state.best)); } catch { /* storage blocked */ }
        }
        flash = Math.max(0, flash - dt);
        draw();
        if (state.phase === "playing") frame = requestAnimationFrame(tick);
      };
      frame = requestAnimationFrame(tick);
    }
    const onVisibility = () => { if (document.hidden) cancelAnimationFrame(frame); else if (state.phase === "playing") loop(); };
    document.addEventListener("visibilitychange", onVisibility);

    function draw() {
      if (!ctx || !width || !height) return;
      const px = (x: number) => x * width, py = (y: number) => y * height;
      ctx.clearRect(0, 0, width, height);
      // Pitch stripes and a penalty spot.
      for (let i = 0; i < 6; i++) { ctx.fillStyle = i % 2 ? "#eaf4ec" : "#e1efe4"; ctx.fillRect(0, (i * height) / 6, width, height / 6 + 1); }
      ctx.fillStyle = "#fff"; ctx.beginPath(); ctx.arc(px(0.5), py(SPOT_Y), 3, 0, Math.PI * 2); ctx.fill();
      // Goal: net behind the line, then posts and crossbar in club colours.
      const left = px(GOAL_LEFT), right = px(GOAL_RIGHT), line = py(GOAL_LINE), back = height - 2;
      const shake = state.last === "goal" && flash > 0 ? Math.sin(flash * 60) * 2 : 0;
      ctx.strokeStyle = "#c9d3dc"; ctx.lineWidth = 1;
      for (let x = left; x <= right; x += 12) { ctx.beginPath(); ctx.moveTo(x + shake, line); ctx.lineTo(x + shake, back); ctx.stroke(); }
      for (let y = line; y <= back; y += 8) { ctx.beginPath(); ctx.moveTo(left, y + shake / 2); ctx.lineTo(right, y + shake / 2); ctx.stroke(); }
      ctx.lineWidth = 5; ctx.lineCap = "round";
      ctx.strokeStyle = RED; ctx.beginPath(); ctx.moveTo(left, back); ctx.lineTo(left, line); ctx.stroke();
      ctx.strokeStyle = BLUE; ctx.beginPath(); ctx.moveTo(right, back); ctx.lineTo(right, line); ctx.stroke();
      ctx.strokeStyle = "#fff"; ctx.lineWidth = 4; ctx.beginPath(); ctx.moveTo(left, line); ctx.lineTo(right, line); ctx.stroke();
      // Ziggy on the line, leaning towards the dive.
      const size = spriteHeight();
      const lean = Math.max(-0.35, Math.min(0.35, (state.targetX - state.keeperX) * 4));
      if (ziggy.complete && ziggy.naturalWidth) {
        const w = (size * ziggy.naturalWidth) / ziggy.naturalHeight;
        ctx.save(); ctx.translate(px(state.keeperX), line + 2); ctx.rotate(lean);
        ctx.drawImage(ziggy, -w / 2, -size, w, size);
        ctx.restore();
      }
      if (state.shot) {
        const ball = ballAt(state.shot);
        drawBall(ctx, px(ball.x), py(ball.y), 5 + ball.depth * (BALL_RADIUS - 5));
      }
      // Scoreboard and prompts.
      ctx.textAlign = "left"; ctx.textBaseline = "top";
      ctx.font = "700 13px system-ui, -apple-system, sans-serif"; ctx.fillStyle = BLUE;
      if (state.phase === "playing") {
        ctx.fillText(`Saves ${state.saves}  ·  Goals ${state.goals}`, 12, 10);
        ctx.textAlign = "right"; ctx.fillStyle = "#454545";
        ctx.fillText(`Best streak ${state.best}`, width - 12, 10);
      }
      if (state.phase === "idle") banner("Tap or click to play in goal", "Move to keep Ziggy between the ball and the net");
      else if (flash > 0 && state.last) banner(state.last === "save" ? (state.streak >= 3 ? `Save! ${state.streak} in a row` : "Save!") : "Goal…", "");
    }

    function banner(title: string, sub: string) {
      if (!ctx) return;
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.font = "800 18px system-ui, -apple-system, sans-serif";
      ctx.fillStyle = state.last === "goal" && state.phase === "playing" ? RED : BLUE;
      ctx.fillText(title, width / 2, height * 0.42);
      if (sub) { ctx.font = "600 12px system-ui, -apple-system, sans-serif"; ctx.fillStyle = "#454545"; ctx.fillText(sub, width / 2, height * 0.42 + 22); }
    }

    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
      el.removeEventListener("pointermove", onMove);
      el.removeEventListener("pointerdown", onDown);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, []);

  return <canvas ref={canvas} className="goalie-game" />;
}

function drawBall(ctx: CanvasRenderingContext2D, x: number, y: number, r: number) {
  ctx.save();
  ctx.fillStyle = "#0002"; ctx.beginPath(); ctx.ellipse(x, y + r * 0.9, r * 0.9, r * 0.3, 0, 0, Math.PI * 2); ctx.fill();
  ctx.fillStyle = "#fff"; ctx.strokeStyle = "#111"; ctx.lineWidth = 1.5;
  ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
  ctx.fillStyle = "#111"; ctx.beginPath();
  for (let i = 0; i < 5; i++) {
    const a = -Math.PI / 2 + (i * 2 * Math.PI) / 5;
    const fn = i ? ctx.lineTo : ctx.moveTo;
    fn.call(ctx, x + Math.cos(a) * r * 0.42, y + Math.sin(a) * r * 0.42);
  }
  ctx.fill();
  ctx.restore();
}
