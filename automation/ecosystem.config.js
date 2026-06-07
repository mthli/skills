// pm2 ecosystem config.
//
// Start:     pm2 start automation/ecosystem.config.js
// Autostart: pm2 save && pm2 startup (then run the printed sudo command)
// Logs:      pm2 logs daily-market-scan
//
// Note: this is a one-shot script, not a long-running daemon — so autorestart:false,
// and cron_restart fires it once at a fixed time; it exits when done (status going
// back to "stopped" is expected). pm2 start also runs it immediately once up front.

module.exports = {
  apps: [
    {
      name: 'daily-market-scan',
      cwd: '/Users/matthew/GitHub/skills',
      script: 'automation/daily-market-scan.sh',
      interpreter: 'bash',

      autorestart: false,          // one-shot task, do not restart after it exits.
      cron_restart: '0 8 * * 2-6', // 08:00 Beijing daily, Tue–Sat (after US Mon–Fri close).
      time: true,                  // prefix log lines with timestamps.

      // pm2 cron may run with a minimal PATH; pin where ccp / node live.
      env: {
        PATH: [
          '/opt/homebrew/bin',
          '/Users/matthew/.nvm/versions/node/v25.6.1/bin',
          '/usr/bin', '/bin', '/usr/sbin', '/sbin',
        ].join(':'),
      },
    },
  ],
}
