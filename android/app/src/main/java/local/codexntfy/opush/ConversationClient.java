package local.codexntfy.opush;

import android.content.Context;
import android.net.Network;
import android.net.Uri;
import android.os.SystemClock;

import org.json.JSONException;
import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.Proxy;
import java.net.URL;
import java.nio.charset.StandardCharsets;

final class ConversationClient {
    private final Context context;

    ConversationClient(Context context) {
        this.context = context.getApplicationContext();
    }

    JSONObject read(String eventId, String revision) throws IOException, JSONException {
        Uri target = Uri.parse(TailscaleReminderService.HOST + "/v1/conversation").buildUpon()
                .appendQueryParameter("event_id", eventId)
                .appendQueryParameter("revision", revision)
                .build();
        return request(target.toString(), null);
    }

    JSONObject catalog() throws IOException, JSONException {
        return request(TailscaleReminderService.HOST + "/v1/threads", null);
    }

    JSONObject projects(String revision) throws IOException, JSONException {
        String url = Uri.parse(TailscaleReminderService.HOST + "/v1/projects").buildUpon()
                .appendQueryParameter("revision", revision).build().toString();
        return request(url, null);
    }

    JSONObject create(String project, String requestId) throws IOException, JSONException {
        return request(TailscaleReminderService.HOST + "/v1/thread/new",
                new JSONObject().put("project_id", project).put("request_id", requestId));
    }

    JSONObject setArchive(String thread, boolean archived) throws IOException, JSONException {
        return request(TailscaleReminderService.HOST + (archived ? "/v1/thread/archive" : "/v1/thread/unarchive"),
                new JSONObject().put("thread_id", thread));
    }

    JSONObject move(String thread, String project) throws IOException, JSONException {
        return request(TailscaleReminderService.HOST + "/v1/thread/move",
                new JSONObject().put("thread_id", thread).put("project_id", project));
    }

    JSONObject rename(String thread, String title) throws IOException, JSONException {
        return request(TailscaleReminderService.HOST + "/v1/thread/rename",
                new JSONObject().put("thread_id", thread).put("title", title));
    }

    JSONObject renameProject(String project, String name) throws IOException, JSONException {
        return request(TailscaleReminderService.HOST + "/v1/project/rename",
                new JSONObject().put("project_id", project).put("name", name));
    }

    JSONObject filePreview(String thread, String path, int line) throws IOException, JSONException {
        String url = Uri.parse(TailscaleReminderService.HOST + "/v1/thread/file").buildUpon()
                .appendQueryParameter("thread_id", thread).appendQueryParameter("path", path)
                .appendQueryParameter("line", Integer.toString(line)).build().toString();
        return request(url, null);
    }

    JSONObject changeFile(String thread, String turn, String path, String action) throws IOException, JSONException {
        return request(TailscaleReminderService.HOST + "/v1/thread/file/change",
                new JSONObject().put("thread_id", thread).put("turn_id", turn)
                        .put("path", path).put("action", action));
    }

    JSONObject accountUsage() throws IOException, JSONException {
        return request(TailscaleReminderService.HOST + "/v1/account/usage", null, 34000);
    }

    long measureLatency() throws IOException {
        Network network = TailscaleReminderService.tailscaleNetwork(context);
        if (network == null) {
            throw new IOException("Tailscale disconnected");
        }
        URL target = new URL(TailscaleReminderService.HOST + "/health");
        HttpURLConnection connection = (HttpURLConnection) network.openConnection(target, Proxy.NO_PROXY);
        try {
            connection.setUseCaches(false);
            connection.setInstanceFollowRedirects(false);
            connection.setConnectTimeout(4500);
            connection.setReadTimeout(8000);
            long start = SystemClock.elapsedRealtime();
            if (connection.getResponseCode() != HttpURLConnection.HTTP_OK) {
                throw new IOException("Computer did not acknowledge latency probe");
            }
            return SystemClock.elapsedRealtime() - start;
        } finally {
            connection.disconnect();
        }
    }

    JSONObject fork(String thread, String turn, String requestId) throws IOException, JSONException {
        return request(TailscaleReminderService.HOST + "/v1/thread/fork",
                new JSONObject().put("thread_id", thread).put("turn_id", turn).put("request_id", requestId));
    }

    JSONObject models(String thread) throws IOException, JSONException {
        String url = Uri.parse(TailscaleReminderService.HOST + "/v1/models").buildUpon()
                .appendQueryParameter("thread_id", thread).build().toString();
        JSONObject result = request(url, null);
        if (result == null) {
            throw new IOException("Computer model list unavailable");
        }
        return result;
    }

    JSONObject readThread(String thread, String revision) throws IOException, JSONException {
        Uri.Builder url = Uri.parse(TailscaleReminderService.HOST + "/v1/thread").buildUpon()
                .appendQueryParameter("thread_id", thread);
        JSONObject page = request(url.build().buildUpon().appendQueryParameter("revision", revision)
                .build().toString(), null);
        if (page == null || page.optBoolean("unchanged")) {
            return page;
        }
        String version = page.getString("revision");
        String cacheRevision = page.optString("cache_revision", version);
        long modified = page.getLong("modified_at");
        JSONArray turns = page.getJSONArray("turns");
        int next = page.getInt("next_offset");
        while (next >= 0) {
            Uri continuation = url.build().buildUpon().appendQueryParameter("revision", version)
                    .appendQueryParameter("offset", Integer.toString(next)).build();
            page = request(continuation.toString(), null);
            if (page == null || page.optBoolean("changed") || !version.equals(page.optString("revision"))) {
                throw new IOException("Conversation changed during download");
            }
            JSONArray batch = page.getJSONArray("turns");
            for (int i = 0; i < batch.length(); i++) {
                turns.put(batch.getJSONObject(i));
            }
            if (page.getInt("next_offset") >= 0 && page.getInt("next_offset") <= next) {
                throw new IOException("Invalid conversation offset");
            }
            next = page.getInt("next_offset");
        }
        return new JSONObject().put("revision", version).put("cache_revision", cacheRevision)
                .put("modified_at", modified).put("turns", turns);
    }

    JSONObject send(String target, boolean byThread, String id, String text, String model, String effort, JSONArray attachments)
            throws IOException, JSONException {
        JSONObject data = new JSONObject().put(byThread ? "thread_id" : "event_id", target)
                .put("request_id", id).put("text", text);
        if (!model.isEmpty()) {
            data.put("model", model);
        }
        if (!effort.isEmpty()) {
            data.put("effort", effort);
        }
        if (attachments.length() > 0) data.put("attachments", attachments);
        return request(TailscaleReminderService.HOST + "/v1/reply", data);
    }

    JSONObject interrupt(String thread, String turn) throws IOException, JSONException {
        return request(TailscaleReminderService.HOST + "/v1/thread/interrupt",
                new JSONObject().put("thread_id", thread).put("turn_id", turn));
    }

    private JSONObject request(String url, JSONObject body) throws IOException, JSONException {
        return request(url, body, body == null ? 12000 : 34000);
    }

    private JSONObject request(String url, JSONObject body, int timeout) throws IOException, JSONException {
        Network VPN = TailscaleReminderService.tailscaleNetwork(context);
        if (VPN == null) {
            throw new IOException("Tailscale disconnected");
        }
        HttpURLConnection http = (HttpURLConnection) VPN.openConnection(new URL(url), Proxy.NO_PROXY);
        try {
            http.setConnectTimeout(4500);
            http.setReadTimeout(timeout);
            http.setUseCaches(false);
            http.setInstanceFollowRedirects(false);
            if (body != null) {
                byte[] content = body.toString().getBytes(StandardCharsets.UTF_8);
                http.setRequestMethod("POST");
                http.setDoOutput(true);
                http.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                http.setFixedLengthStreamingMode(content.length);
                try (OutputStream output = http.getOutputStream()) {
                    output.write(content);
                }
            }
            int status = http.getResponseCode();
            if (status == HttpURLConnection.HTTP_NOT_FOUND && body == null) {
                return null;
            }
            if (status != HttpURLConnection.HTTP_OK) {
                throw new IOException("Conversation HTTP " + status);
            }
            try (InputStream stream = http.getInputStream();
                 ByteArrayOutputStream content = new ByteArrayOutputStream()) {
                byte[] block = new byte[8192];
                int count;
                while ((count = stream.read(block)) != -1) {
                    if (content.size() + count > 16 * 1024 * 1024) {
                        throw new IOException("Conversation response too large");
                    }
                    content.write(block, 0, count);
                }
                return new JSONObject(new String(content.toByteArray(), StandardCharsets.UTF_8));
            }
        } finally {
            http.disconnect();
        }
    }
}
