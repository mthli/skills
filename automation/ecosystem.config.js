// pm2 ecosystem config.
//
// Start:     pm2 start automation/ecosystem.config.js
// Autostart: pm2 save && pm2 startup (then run the printed sudo command)
// Logs:      pm2 logs APP_NAME (e.g. "pm2 logs daily-market-scan")
//
// Note: this is a one-shot script, not a long-running daemon — so autorestart:false,
// and cron_restart fires it once at a fixed time; it exits when done (status going
// back to "stopped" is expected). pm2 start also runs it immediately once up front.

const path = require('path')

module.exports = {
  apps: [
    {
      name: 'daily-market-scan',
      script: path.join(__dirname, 'daily-market-scan.sh'),
      interpreter: 'bash',

      autorestart: false,          // one-shot task, do not restart after it exits.
      cron_restart: '0 8 * * 2-6', // 08:00 Beijing daily, Tue–Sat (after US Mon–Fri close).
      time: true,                  // prefix log lines with timestamps.

      // On boot, launchd resurrects the pm2 daemon with a minimal PATH, so pin the
      // dirs the job needs up front, then fall back to the full PATH captured at
      // `pm2 start` time (covers claude, node, python, and anything added later).
      env: {
        PATH: [
          path.dirname(process.execPath),    // node bin dir of the pm2 process — tracks nvm, no version pinned; kept ahead of homebrew node.
          `${process.env.HOME}/.local/bin`,  // claude (native).
          '/opt/homebrew/bin',               // tmux, jq.
          process.env.PATH,                  // belt-and-suspenders: full PATH at start time.
        ].join(path.delimiter),
      },
    },
  ],
}
