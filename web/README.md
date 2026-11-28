# CDCI recruiter demo UI

Run the API and worker separately using api/README.md.

\`\`\`powershell
cd web
npm install
$env:NEXT_PUBLIC_API_URL = "http://127.0.0.1:8000"
$env:NEXT_PUBLIC_CDCI_API_KEY = "YOUR_CDCI_API_KEY"
npm run dev
\`\`\`

Open http://localhost:3000. NEXT_PUBLIC_CDCI_API_KEY is a browser-visible demo credential, not a production secret. For production, use a backend-for-frontend session or OIDC flow; never put a privileged service key in a public Next.js environment variable.
