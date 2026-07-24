// PM2 process file for the Timesheet app (AWS-native).
//   pm2 start ecosystem.config.cjs && pm2 save && pm2 startup
module.exports = {
  apps: [
    {
      name: "ajace-timesheet",
      cwd: "/home/ubuntu/ajace-timesheet-platform/app",
      script: "npm",
      args: "run start",              // next start -p 3009
      env: { NODE_ENV: "production" },
      max_memory_restart: "600M",
      autorestart: true,
    },
  ],
};
