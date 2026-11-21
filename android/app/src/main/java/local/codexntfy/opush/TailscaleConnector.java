package local.codexntfy.opush;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;

final class TailscaleConnector {
    private static final String PACKAGE = "com.tailscale.ipn";
    private static final String CONNECT = "com.tailscale.ipn.CONNECT_VPN";
    private static final String DISCONNECT = "com.tailscale.ipn.DISCONNECT_VPN";

    private TailscaleConnector() {}

    static boolean requestConnection(Activity activity) {
        return request(activity, CONNECT);
    }

    static boolean requestDisconnection(Activity activity) {
        return request(activity, DISCONNECT);
    }

    private static boolean request(Activity activity, String action) {
        PackageManager packages = activity.getPackageManager();
        Intent command = new Intent(action).setPackage(PACKAGE);
        try {
            if (packages.resolveActivity(command, PackageManager.MATCH_DEFAULT_ONLY) != null) {
                activity.startActivity(command);
                return true;
            }
            for (ResolveInfo receiver : packages.queryBroadcastReceivers(command, 0)) {
                if (receiver.activityInfo != null && receiver.activityInfo.exported) {
                    activity.sendBroadcast(command.setClassName(PACKAGE, receiver.activityInfo.name));
                    return true;
                }
            }
        } catch (ActivityNotFoundException | SecurityException ignored) {
            // The installed release may only support opening its own connection screen.
        }
        return false;
    }

    static boolean openApp(Activity activity) {
        Intent launch = activity.getPackageManager().getLaunchIntentForPackage(PACKAGE);
        if (launch != null) {
            try {
                activity.startActivity(launch);
                return true;
            } catch (ActivityNotFoundException | SecurityException ignored) {
                // Tailscale can be unavailable or disabled on this device.
            }
        }
        return false;
    }
}
