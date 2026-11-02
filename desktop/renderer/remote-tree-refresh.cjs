'use strict';

class RecursiveRefreshTimer {
  constructor({run, intervalMs, setTimer = setTimeout, clearTimer = clearTimeout, onError = () => {}} = {}) {
    if (typeof run !== 'function') throw new TypeError('run is required');
    if (!Number.isFinite(intervalMs) || intervalMs <= 0) throw new TypeError('intervalMs must be positive');
    this.run = run;
    this.intervalMs = intervalMs;
    this.setTimer = setTimer;
    this.clearTimer = clearTimer;
    this.onError = onError;
    this.timer = null;
    this.running = false;
  }

  start() {
    this.running = true;
    this.arm();
  }

  stop() {
    this.running = false;
    if (this.timer !== null) this.clearTimer(this.timer);
    this.timer = null;
  }

  arm() {
    if (!this.running || this.timer !== null) return;
    this.timer = this.setTimer(async () => {
      this.timer = null;
      try { await this.run(); }
      catch (error) { this.onError(error); }
      finally { if (this.running) this.arm(); }
    }, this.intervalMs);
  }
}

module.exports = {RecursiveRefreshTimer};
