package local.codexntfy.opush;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.ConnectivityManager;
import android.net.LinkAddress;
import android.net.LinkProperties;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.Uri;
import android.os.IBinder;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.Proxy;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.RejectedExecutionException;

public final class TailscaleReminderService extends Service {
    static final String PREFS = "tailscale_receiver";
    static final String HOST = BuildConfig.TAILSCALE_ENDPOINT;
    static final String REMINDER_CHANNEL = "tailscale_completion";
    private static final String PHONE_IP = BuildConfig.TAILSCALE_PHONE_IP;
    private static final String SERVICE_CHANNEL = "tailscale_connection";
    private static final int SERVICE_NOTIFICATION = 1;
    private static volatile TailscaleReminderService current;

    enum Status { STOPPED, CONNECTING, CONNECTED, WAITING_FOR_VPN, BLOCKED, RETRYING }

    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private final ExecutorService historyWorker = Executors.newSingleThreadExecutor();
    private volatile HttpURLConnection connection;
    private volatile Status status = Status.CONNECTING;
    private NotificationManager notifications;
    private SharedPreferences delivered;

    static void createChannels(NotificationManager manager) {
        manager.createNotificationChannel(new NotificationChannel(
                SERVICE_CHANNEL, "接收服务", NotificationManager.IMPORTANCE_LOW));
        NotificationChannel reminders = new NotificationChannel(
                REMINDER_CHANNEL, "Codex 完成提醒", NotificationManager.IMPORTANCE_HIGH);
        reminders.setLockscreenVisibility(Notification.VISIBILITY_PUBLIC);
        manager.createNotificationChannel(reminders);
    }

    static boolean canPost(NotificationManager manager) {
        return manager.areNotificationsEnabled()
                && manager.getNotificationChannel(REMINDER_CHANNEL).getImportance()
                != NotificationManager.IMPORTANCE_NONE;
    }

    static Status status() {
        TailscaleReminderService service = current;
        return service == null ? Status.STOPPED : service.status;
    }

    @Override
    public void onCreate() {
        super.onCreate();
        notifications = getSystemService(NotificationManager.class);
        delivered = getSharedPreferences(ReminderHistory.PREFS, MODE_PRIVATE);
        createChannels(notifications);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (HOST.isEmpty()) {
            stopSelf();
            return START_NOT_STICKY;
        }
        if (current == this) {
            return START_STICKY;
        }
        startForeground(SERVICE_NOTIFICATION, notification(SERVICE_CHANNEL, "Codex 提醒接收已开启", null)
                .setContentText("通过 Tailscale 等待电脑的完成提醒")
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .build());
        current = this;
        updateStatus(Status.CONNECTING);
        worker.execute(this::listen);
        return START_STICKY;
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onDestroy() {
        current = null;
        worker.shutdownNow();
        historyWorker.shutdownNow();
        HttpURLConnection pending = connection;
        if (pending != null) {
            // disconnect() can wait on socket I/O. Keep it off the UI thread.
            new Thread(pending::disconnect).start();
        }
        getSharedPreferences(PREFS, MODE_PRIVATE).edit().putString("status", Status.STOPPED.name()).apply();
        super.onDestroy();
    }

    private void updateStatus(Status next) {
        if (current == this) {
            status = next;
            SharedPreferences state = getSharedPreferences(PREFS, MODE_PRIVATE);
            if (!next.name().equals(state.getString("status", ""))) {
                state.edit().putString("status", next.name()).apply();
            }
        }
    }

    private void listen() {
        long retryDelay = 1000;
        while (current == this) {
            try {
                if (!canPost(notifications)) {
                    updateStatus(Status.BLOCKED);
                    Thread.sleep(3000);
                    continue;
                }
                Network network = tailscaleNetwork(this);
                if (network == null) {
                    updateStatus(Status.WAITING_FOR_VPN);
                    Thread.sleep(3000);
                    continue;
                }
                JSONObject message = request(network, "/v1/reminders", null);
                updateStatus(Status.CONNECTED);
                if (message != null && current == this && postReminder(message)) {
                    String eventId = message.getString("event_id");
                    JSONObject ack = new JSONObject().put("event_id", eventId);
                    request(network, "/v1/ack", ack);
                    cacheConversation(ThreadStore.threadFromEvent(eventId));
                }
                retryDelay = 1000;
            } catch (InterruptedException stopped) {
                Thread.currentThread().interrupt();
                return;
            } catch (IOException | JSONException | SecurityException error) {
                updateStatus(Status.RETRYING);
                try {
                    Thread.sleep(retryDelay);
                } catch (InterruptedException stopped) {
                    Thread.currentThread().interrupt();
                    return;
                }
                retryDelay = Math.min(15000, retryDelay * 2);
            }
        }
    }

    private void cacheConversation(String thread) {
        if (thread == null || current != this) {
            return;
        }
        try {
            historyWorker.execute(() -> {
                ThreadStore store = new ThreadStore(this);
                JSONObject saved = store.read(thread);
                try {
                    JSONObject updated = new ConversationClient(this).readThread(thread,
                            saved == null ? "" : saved.optString("revision"));
                    if (updated != null && !updated.optBoolean("unchanged")) {
                        store.save(thread, updated);
                    }
                } catch (IOException | JSONException | SecurityException ignored) {
                    // The conversation can retry independently when opened.
                }
            });
        } catch (RejectedExecutionException ignored) {
            // The receiver was stopped while the reminder was being acknowledged.
        }
    }

    static Network tailscaleNetwork(Context context) {
        if (HOST.isEmpty()) {
            return null;
        }
        ConnectivityManager manager = context.getSystemService(ConnectivityManager.class);
        Network active = manager.getActiveNetwork();
        if (active == null) {
            return null;
        }
        NetworkCapabilities capabilities = manager.getNetworkCapabilities(active);
        LinkProperties link = manager.getLinkProperties(active);
        if (capabilities == null || !capabilities.hasTransport(NetworkCapabilities.TRANSPORT_VPN)
                || link == null) {
            return null;
        }
        for (LinkAddress address : link.getLinkAddresses()) {
            if (PHONE_IP.equals(address.getAddress().getHostAddress())) {
                return active;
            }
        }
        return null;
    }

    private JSONObject request(Network network, String path, JSONObject body)
            throws IOException, JSONException {
        HttpURLConnection http = (HttpURLConnection) network.openConnection(
                new URL(HOST + path), Proxy.NO_PROXY);
        connection = http;
        try {
            http.setConnectTimeout(4000);
            http.setReadTimeout(26000);
            http.setInstanceFollowRedirects(false);
            http.setUseCaches(false);
            if (body != null) {
                byte[] bytes = body.toString().getBytes(StandardCharsets.UTF_8);
                http.setRequestMethod("POST");
                http.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                http.setDoOutput(true);
                http.setFixedLengthStreamingMode(bytes.length);
                try (OutputStream output = http.getOutputStream()) {
                    output.write(bytes);
                }
            }
            int status = http.getResponseCode();
            if (status == HttpURLConnection.HTTP_NO_CONTENT) {
                return null;
            }
            if (status != HttpURLConnection.HTTP_OK || body != null) {
                throw new IOException("Unexpected receiver response");
            }
            try (InputStream input = http.getInputStream()) {
                ByteArrayOutputStream buffer = new ByteArrayOutputStream();
                byte[] chunk = new byte[1024];
                int count;
                while ((count = input.read(chunk)) != -1) {
                    if (buffer.size() + count > 16384) {
                        throw new IOException("Response is too large");
                    }
                    buffer.write(chunk, 0, count);
                }
                return new JSONObject(new String(buffer.toByteArray(), StandardCharsets.UTF_8));
            }
        } finally {
            connection = null;
            http.disconnect();
        }
    }

    private boolean postReminder(JSONObject message) throws JSONException, IOException {
        // A stopped worker can still be finishing its write when a new service starts.
        synchronized (delivered) {
            return postAndRecord(message);
        }
    }

    private boolean postAndRecord(JSONObject message) throws JSONException, IOException {
        long now = System.currentTimeMillis();
        long expires = message.getLong("expires_at_ms");
        if (expires <= now) {
            // Let the server age out the offer. Clock skew must not cause a tight loop.
            throw new IOException("Expired reminder");
        }
        String id = message.getString("event_id");
        if (delivered.contains(id)) {
            return true;
        }
        if (!canPost(notifications) || current != this) {
            return false;
        }
        String title = message.getString("title");
        String body = message.getString("message");
        String previousHistory = delivered.getString(ReminderHistory.KEY, null);
        String history = ReminderHistory.prepend(previousHistory,
                new ReminderHistory.Entry(id, title, body, now));
        notifications.notify(id, 0, notification(REMINDER_CHANNEL, title, id)
                .setContentText(message.optString("summary", body.replace('\n', ' ')))
                .setStyle(new Notification.BigTextStyle().bigText(body))
                .setAutoCancel(true)
                .setOnlyAlertOnce(true)
                .setVisibility(Notification.VISIBILITY_PUBLIC)
                .build());
        SharedPreferences.Editor saved = delivered.edit();
        for (Map.Entry<String, ?> item : delivered.getAll().entrySet()) {
            if (item.getValue() instanceof Long && (Long) item.getValue() <= now) {
                saved.remove(item.getKey());
            }
        }
        if (!saved.putLong(id, expires).putString(ReminderHistory.KEY, history).commit()) {
            // A failed commit can still update in-process preferences. Allow the offer to retry.
            delivered.edit().remove(id).putString(ReminderHistory.KEY, previousHistory).apply();
            throw new IOException("Could not store the posted event ID");
        }
        return true;
    }

    private Notification.Builder notification(String channel, String title, String reminderId) {
        Intent target = new Intent(this, MainActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        if (reminderId == null) {
            target.setAction(MainActivity.ACTION_OPEN_CONNECTION);
        } else {
            target.setAction(MainActivity.ACTION_OPEN_REMINDER)
                    .setData(Uri.fromParts("codex-reminder", reminderId, null));
        }
        PendingIntent open = PendingIntent.getActivity(this, 0, target,
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        return new Notification.Builder(this, channel)
                .setSmallIcon(R.drawable.ic_notification_envelope)
                .setContentTitle(title)
                .setContentIntent(open);
    }
}
