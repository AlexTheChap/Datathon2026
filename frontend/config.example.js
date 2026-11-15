// Shape of frontend/config.js, which scripts/setup_cognito.py writes (git-ignored, it holds the demo password).
// Without config.js the app runs in local dev mode (no password, X-Actor-Id header; API with AF_AUTH_MODE=dev).
window.AF_CONFIG = {
  auth: "cognito",
  region: "us-east-1",
  userPoolId: "us-east-1_XXXXXXXXX",
  clientId: "xxxxxxxxxxxxxxxxxxxxxxxxxx",
  demoAccounts: [
    { persona: "robert", name: "Robert", email: "robert.demo@alphaforge.example", password: "<from setup_cognito.py>",
      summary: "Conservative investor, 59, healthcare professional." },
  ],
};
