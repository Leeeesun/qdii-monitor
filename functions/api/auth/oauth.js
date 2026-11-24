const OAUTH_AUTH_URL = "https://auth.youranzi.de/authorize";
const OAUTH_CLIENT_ID = "qdii";

export async function onRequestGet(context) {
  const { request, env } = context;
  const url = new URL(request.url);
  if (!env.OAUTH_CLIENT_SECRET) {
    return Response.redirect(`${url.origin}/#oauth_error=missing_secret`, 302);
  }
  const state = crypto.randomUUID();
  const redirectUri = `${url.origin}/api/auth/callback`;
  const target = new URL(OAUTH_AUTH_URL);
  target.searchParams.set("client_id", OAUTH_CLIENT_ID);
  target.searchParams.set("redirect_uri", redirectUri);
  target.searchParams.set("response_type", "code");
  target.searchParams.set("state", state);

  return new Response(null, {
    status: 302,
    headers: {
      Location: target.toString(),
      "Set-Cookie": `qdii_oauth_state=${state}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=600`,
      "Cache-Control": "no-store, no-cache, must-revalidate"
    }
  });
}
