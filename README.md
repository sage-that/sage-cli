# Sage CLI

Terminal client for [Sage](https://sagethat.com) — an interactive thought companion.

```bash
pip install git+https://github.com/sage-that/sage-cli.git
sage login
sage
```

## Usage

| Command | Description |
|---------|-------------|
| `sage` | Start interactive chat session |
| `sage --deep` | Deep analysis mode |
| `sage --debug` | Show investigation query details |
| `sage --local` | Connect to localhost (dev mode) |
| `sage login` | Authenticate via Google OAuth (browser) |
| `sage logout` | Remove cached credentials |

Type `/page`, `/patterns`, `/activity`, or `/explore` during a session.

## Environment

| Variable | Default | Description |
|----------|---------|-------------|
| `SAGE_API_URL` | `https://api.dev.sagethat.com` | Backend API base URL |
| `SAGE_COGNITO_CLIENT_ID` | `72f3d84edho6neu063n4ab6cb` | Cognito User Pool Client ID |
| `SAGE_COGNITO_DOMAIN` | `dev.auth.sagethat.com` | Cognito hosted UI domain |

## Requirements

Python 3.11+
