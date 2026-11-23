const OAUTH_TOKEN_URL = "https://auth.youranzi.de/oauth/token";
const OAUTH_USER_URL = "https://auth.youranzi.de/api/user";
const OAUTH_CLIENT_ID = "qdii";
const encoder = new TextEncoder();

function toHex(buffer) {
  return [...new Uint8Array(buffer)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function toBase64Url(str) {
  const bytes = encoder.encode(str);
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function signToken(payload, secret) {
  const body = toBase64Url(JSON.stringify(payload));
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, encoder.encode(body));
  return `${body}.${toHex(sig)}`;
}

function redirectErr(origin, code) {
  return new Response(null, {
    status: 302,
    headers: {
      Location: `${origin}/#oauth_error=${encodeURIComponent(code)}`,
      "Set-Cookie": "qdii_oauth_state=; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=0"
    }
  });
}

export async function onRequestGet(context) {
  const { request, env } = context;
  const url = new URL(request.url);

  if (!env.OAUTH_CLIENT_SECRET) return redirectErr(url.origin, "missing_secret");
  const errParam = url.searchParams.get("error");
  if (errParam) return redirectErr(url.origin, errParam);

  const code = url.searchParams.get("code") || "";
  const state = url.searchParams.get("state") || "";
  const cookie = request.headers.get("Cookie") || "";
  const m = cookie.match(/(?:^|;\s*)qdii_oauth_state=([^;]+)/);
  const savedState = m ? decodeURIComponent(m[1]) : "";
  if (!code || !state || !savedState || state !== savedState) {
    return redirectErr(url.origin, "invalid_state");
  }

  let accessToken = "";
  try {
    const tokenRes = await fetch(OAUTH_TOKEN_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        client_id: OAUTH_CLIENT_ID,
        client_secret: env.OAUTH_CLIENT_SECRET,
        code
      })
    });
    if (!tokenRes.ok) return redirectErr(url.origin, "token_failed");
    const tokenJson = await tokenRes.json();
    accessToken = tokenJson.access_token || "";
  } catch {
    return redirectErr(url.origin, "token_error");
  }

  if (!accessToken) return redirectErr(url.origin, "empty_token");

  let oauthUser = null;
  try {
    const userRes = await fetch(OAUTH_USER_URL, {
      headers: { Authorization: `Bearer ${accessToken}` }
    });
    if (!userRes.ok) return redirectErr(url.origin, "user_failed");
    oauthUser = await userRes.json();
  } catch {
    return redirectErr(url.origin, "user_error");
  }

  if (!oauthUser || String(oauthUser.id) !== "1") {
    return redirectErr(url.origin, "unauthorized_user");
  }

  const secret = env.JWT_SECRET || "qdii-monitor-default-secret-change-me";
  const exp = Date.now() + 30 * 24 * 60 * 60 * 1000;
  const user = { username: "Lesun", role: "admin", exp };
  const token = await signToken(user, secret);

  return new Response(null, {
    status: 302,
    headers: {
      Location: `${url.origin}/#oauth_token=${encodeURIComponent(token)}`,
      "Set-Cookie": "qdii_oauth_state=; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=0"
    }
  });
}
