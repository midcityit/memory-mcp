export const runtime = "edge";

export default function LoginPage() {
  return (
    <main style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", minHeight: "80vh" }}>
      <h1>Memory Twin</h1>
      <p>Sign in to browse agent memories.</p>
      <a
        href="/api/auth/signin/azure-ad"
        style={{ padding: "0.75rem 1.5rem", background: "#0078d4", color: "white", textDecoration: "none", borderRadius: "4px" }}
      >
        Sign in with Microsoft
      </a>
    </main>
  );
}
