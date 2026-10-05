package com.keneristudios.interlux.agent

import android.content.Context
import org.json.JSONObject

/**
 * The app's own credential for its daemon (step 1b).
 *
 * The daemon mints an owner token to whoever handshakes first on an
 * empty store — normally this app at service start. The token lives in
 * private SharedPreferences (this uid only, no backup, never logged).
 *
 * Auth is a handshake property: the daemon maps the SOCKET to the agent
 * at `initialize`, so every RPC below runs inside one session —
 * handshake (with auth when held), then the call, then close.
 *
 * Recovery is explicit, never silent: unknown credentials are dropped
 * so the next call re-bootstraps (empty store) or reports pairing
 * required (another owner holds it). Callers show the Agents path on
 * null rather than hanging.
 */
object AgentAuth {

    private const val PREFS = "agent_auth"
    private const val KEY_AGENT_ID = "agent_id"
    private const val KEY_TOKEN = "token"

    data class Creds(val agentId: String, val token: String)

    fun stored(context: Context): Creds? {
        val prefs = context.getSharedPreferences(
            PREFS, Context.MODE_PRIVATE)
        val id = prefs.getString(KEY_AGENT_ID, null)
        val token = prefs.getString(KEY_TOKEN, null)
        if (id.isNullOrEmpty() || token.isNullOrEmpty()) return null
        return Creds(id, token)
    }

    fun clear(context: Context) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit().clear().apply()
    }

    private fun store(context: Context, agentId: String, token: String) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit()
            .putString(KEY_AGENT_ID, agentId)
            .putString(KEY_TOKEN, token)
            .apply()
    }

    private fun helloParams(creds: Creds?): Map<String, Any?> {
        val base = mapOf<String, Any?>(
            "clientInfo" to mapOf("name" to "interlux-app", "version" to "1"),
        )
        if (creds == null) return base
        return base + mapOf("auth" to mapOf(
            "agent_id" to creds.agentId,
            "token" to creds.token,
        ))
    }

    /**
     * One owner-authed RPC. Returns null when pairing is required first.
     * Throws AgentWs.RpcError on transport/daemon failure.
     */
    fun rpc(context: Context, method: String,
            params: Map<String, Any?> = emptyMap()): JSONObject? {
        AgentWs.session().use { session ->
            var creds = stored(context)
            var hello = session.call("initialize", helloParams(creds))
            if (hello.has("token")) {
                // Freshly minted (empty store, or store wiped since save):
                // adopt and continue on this same socket.
                val id = hello.optString("agent_id", "")
                val token = hello.optString("token", "")
                if (id.isEmpty() || token.isEmpty()) return null
                store(context, id, token)
                creds = Creds(id, token)
                hello = session.call("initialize", helloParams(creds))
            }
            if (!hello.optBoolean("paired", false)) {
                // Unknown credentials (revoked, or another owner holds the
                // store): drop ours so the next call re-bootstraps cleanly
                // instead of failing forever on a dead token.
                if (creds != null) clear(context)
                return null
            }
            return session.call(method, params)
        }
    }
}
