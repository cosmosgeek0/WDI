#!/usr/bin/perl
use strict;
use warnings;
use JSON::PP;
use Time::HiRes qw(time sleep);
use Time::Local qw(timegm);
use Fcntl qw(:flock F_GETFL F_SETFL O_WRONLY O_NONBLOCK);
use Scalar::Util qw(looks_like_number);

my $SAMPLE_MS = 10_000;
my $HEALTH_MS = 60_000;
my $OFFLINE_GRACE_MS = 120_000;
my $RESTART_GRACE_MS = 120_000;
my $HEALTHY_RESET_MS = 300_000;
my $MAX_RESTARTS = 2;
my $POLICY = 'native-evidence-2026-09-08';
my $ADAPTER = 'private-desktop-app-io-2026-09-13';
my $SESSION = '/config/xdg/data/io.getgrass.desktop/store.json';
my $STATE = '/config/run/grass-recovery.json';

sub now_ms { return int(time() * 1000); }
sub read_file {
    open my $file, '<', $_[0] or return undef;
    local $/;
    return <$file>;
}
sub decoded {
    my $value = $_[0];
    return undef unless defined $value;
    return eval { decode_json($value) } // $value;
}
sub stamp {
    my $value = $_[0] // '';
    return undef unless $value =~ /\A(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.(\d+))?Z\z/;
    return eval { timegm($6, $5, $4, $3, $2 - 1, $1) * 1000 + int(('0.' . ($7 // 0)) * 1000) };
}
sub emit {
    my ($ctx, $kind, $value) = @_;
    my $row = {kind => $kind, revision => 2, group_id => $ctx->{group_id},
        instance => $ctx->{instance}, machine => $ctx->{machine}, run => $ctx->{run},
        source_ms => now_ms(), runtime_adapter => $ADAPTER, log_dropped => $ctx->{log_dropped} // 0, %$value};
    $row->{request_ms} = $ctx->{recovery}{requested_ms} if $kind eq 'grass_event' && $ctx->{recovery}{requested_ms};
    my $line = ($kind eq 'grass_sample' ? 'GRASS_SAMPLE_V2 ' : 'GRASS_EVENT_V2 ') . encode_json($row) . "\n";
    $ctx->{log_buffer} //= '';
    if (length($ctx->{log_buffer}) + length($line) <= 65536) { $ctx->{log_buffer} .= $line; }
    else { $ctx->{log_dropped}++; }
    my $written = syswrite($ctx->{output}, $ctx->{log_buffer});
    substr($ctx->{log_buffer}, 0, $written, '') if defined($written) && $written;
}
sub save_recovery {
    my ($ctx) = @_;
    open my $file, '>', "$STATE.tmp" or die "Grass recovery state cannot be written\n";
    print {$file} encode_json({container_run => $ctx->{container_run}, %{$ctx->{recovery}}});
    close $file or die "Grass recovery state cannot be closed\n";
    rename "$STATE.tmp", $STATE or die "Grass recovery state cannot be published\n";
}
sub process {
    for my $path (sort glob '/proc/[0-9]*/comm') {
        next unless (read_file($path) // '') eq "grass-desktop\n";
        my @owner = stat $path;
        next unless @owner && $owner[4] == 1000;
        $path =~ m{/proc/(\d+)/};
        my $pid = 0 + $1;
        my $value = read_file("/proc/$pid/stat") // '';
        my @fields = split /\s+/, (split /\) /, $value, 2)[1] // '';
        my $state = $fields[0];
        next if ($state // '') eq 'Z';
        return ($pid, $state, $fields[19]);
    }
    return (undef, undef);
}
sub native_observation {
    my ($ctx, $pid, $state, $ticks, $session, $at) = @_;
    my $identity = defined($ticks) ? "$pid:$ticks" : undef;
    my @store_stat = stat $SESSION;
    my $status = decoded($session->{'wynd:status'});
    $status = undef if ref($status) || (defined($status) && $status !~ /\A[A-Z_]{1,40}\z/);
    my $native = {observed_ms => $at, process_identity => $identity,
        process_start_ticks => $ticks, cached_status => $status,
        store_modified_ms => @store_stat ? int($store_stat[9] * 1000) : undef,
        status_is_heartbeat => JSON::PP::false};
    # Count only sockets owned by this process. A live TCP transport is useful
    # evidence, but does not prove Grass authentication or provider acceptance.
    my %sockets;
    my @fds = glob "/proc/$pid/fd/*";
    for my $fd (@fds) {
        my $target = readlink $fd;
        $sockets{$1} = 1 if defined($target) && $target =~ /\Asocket:\[(\d+)\]\z/;
    }
    my ($readable, $established) = (0, 0);
    for my $path ("/proc/$pid/net/tcp", "/proc/$pid/net/tcp6") {
        my $table = read_file($path);
        next unless defined $table;
        $readable++;
        for my $line (split /\n/, $table) {
            my @f = split /\s+/, $line =~ s/^\s+//r;
            $established++ if @f > 9 && $f[3] eq '01' && $sockets{$f[9]};
        }
    }
    $native->{established_tcp} = $readable && @fds ? $established : undef;
    my $previous = $ctx->{native_previous};
    if (!$previous || ($previous->{process_identity} // '') ne ($identity // '') || !defined $identity) {
        delete $ctx->{native_disconnected_since_ms};
    } elsif (($previous->{cached_status} // '') ne ($status // '')) {
        # Only an observed transition in this process establishes a native
        # disconnect. A store copied from another run is not a heartbeat.
        if (($previous->{cached_status} // '') eq 'CONNECTED' && ($status // '') eq 'DISCONNECTED') {
            $ctx->{native_disconnected_since_ms} = $at;
        } else {
            delete $ctx->{native_disconnected_since_ms};
        }
    }
    $native->{disconnected_since_ms} = $ctx->{native_disconnected_since_ms}
        if $ctx->{native_disconnected_since_ms};
    $ctx->{native_previous} = $native;
    return $native;
}
sub observe {
    my ($ctx) = @_;
    my $at = now_ms();
    my ($pid, $process_state, $ticks) = process();
    my $health = {observed_ms => $at, pid => $pid, process_state => $process_state,
        process_identity => defined($ticks) ? "$pid:$ticks" : undef,
        state => 'unknown', error => undef, device_id => undef, quality => undef};
    if (!$pid) {
        return {%$health, state => 'client_missing', error => 'Grass process is not running'};
    }
    # Lowercase t is an ordinary ptrace stop during measured socket I/O.
    if (($process_state // '') eq 'T') {
        return {%$health, state => 'client_stalled', error => 'Grass process is suspended'};
    }
    my $session = eval { decode_json(read_file($SESSION) // '') };
    return {%$health, error => 'Grass session store is unavailable or malformed'} unless ref($session) eq 'HASH';
    my $native = native_observation($ctx, $pid, $process_state, $ticks, $session, $at);
    $health->{native} = $native;
    my $device = decoded($session->{'wynd:device_id'});
    $health->{account_id} = decoded($session->{'wynd:user_id'});
    if (!defined($device) || ref($device) || !$device) {
        return {%$health, state => 'starting', error => 'Grass is registering this device'};
    }
    $health->{device_id} = $device;
    if ($native->{disconnected_since_ms} && defined($native->{established_tcp}) && $native->{established_tcp} == 0) {
        return {%$health, state => 'offline', failure_source => 'native_disconnect',
            error => 'Grass changed from CONNECTED to DISCONNECTED in this process and has no established TCP transport'};
    }
    my $network = eval { decode_json(read_file('/run/wdi/ip-policy.json') // '') };
    my $boot = (split /\s+/, read_file('/proc/uptime') // '0')[0];
    if (!(ref($network) eq 'HASH' && ref($network->{scope}) eq 'HASH'
            && ($network->{scope}{group} // '') eq $ctx->{group_id}
            && ($network->{scope}{instance} // '') eq $ctx->{instance}
            && ($network->{scope}{machine} // '') eq $ctx->{machine}
            && ($network->{scope}{runtime} // '') eq $ctx->{runtime}
            && JSON::PP::is_bool($network->{allowed}) && $network->{allowed}
            && looks_like_number($network->{valid_until}) && $boot < $network->{valid_until}
            && looks_like_number($network->{ip_checked_ms}) && abs($at-$network->{ip_checked_ms}) <= 90000
            && valid_ipv4($network->{ip}))) {
        $ctx->{ip} = $ctx->{country} = undef;
        return {%$health, error => 'Current admitted public IP is unavailable'};
    }
    $ctx->{ip} = $network->{ip};
    $ctx->{ip_observed_ms} = $network->{ip_checked_ms};
    $ctx->{country} = ($network->{country} // '') =~ /\A[A-Z]{2}\z/ ? $network->{country} : undef;
    my $provider = eval { decode_json(read_file('/run/wdi/provider-status.json') // '') };
    if (!(ref($provider) eq 'HASH' && ref($provider->{scope}) eq 'HASH'
            && ($provider->{scope}{group} // '') eq $ctx->{group_id}
            && ($provider->{scope}{instance} // '') eq $ctx->{instance}
            && ($provider->{scope}{machine} // '') eq $ctx->{machine}
            && ($provider->{scope}{runtime} // '') eq $ctx->{runtime}
            && ($provider->{device} // '') eq $device
            && ($provider->{ip} // '') eq $ctx->{ip}
            && ($provider->{process_identity} // '') eq ($health->{process_identity} // '')
            && looks_like_number($provider->{observed_ms})
            && abs($at-$provider->{observed_ms}) <= 90000)) {
        return {%$health, telemetry_missing => JSON::PP::true,
            error => 'Fresh provider confirmation from the controller is unavailable'};
    }
    $health->{http} = $provider->{http};
    $health->{provider_observed_ms} = $provider->{observed_ms};
    my $device_row = $provider->{data};
    return {%$health, telemetry_missing => JSON::PP::true,
        error => $provider->{error} // 'Provider device telemetry is unavailable'}
        unless ($provider->{http} // 0) == 200 && ref($device_row) eq 'HASH';
    my $matches = ($device_row->{deviceId} // '') eq $device && ($device_row->{ipAddress} // '') eq ($ctx->{ip} // '');
    $health->{provider_listing} = {source => 'controller retrieveDevice', observed_ms => $provider->{observed_ms},
        device_ip_present => $matches ? JSON::PP::true : JSON::PP::false};
    my $connected_at = stamp($device_row->{lastConnectedAt});
    if (!$matches || !defined($connected_at) || !JSON::PP::is_bool($device_row->{isConnected})
            || !$device_row->{isConnected}) {
        $health->{state} = 'unknown';
        $health->{error} = 'Grass does not currently confirm this device and IP; missing telemetry is not a native failure';
        $health->{telemetry_missing} = JSON::PP::true;
    } elsif (!looks_like_number($device_row->{ipScore}) || $device_row->{ipScore} < 0 || $device_row->{ipScore} > 100) {
        $health->{error} = 'Grass did not report a usable network-quality reading';
    } else {
        $health->{quality} = 0 + $device_row->{ipScore};
        $health->{connected_at_ms} = $connected_at;
        $health->{state} = $health->{quality} > 0 ? 'connected' : 'unknown';
        $health->{error} = 'Grass reports zero quality; explicit provider/IP rejection is unverified' if $health->{quality} == 0;
    }
    # Provider replies are about the process/device that initiated this check.
    # Never attach their result to a replacement process after a slow request.
    my ($current_pid, $current_state, $current_ticks) = process();
    if (($current_pid // 0) != $pid || ($current_ticks // '') ne ($ticks // '') || ($current_state // '') eq 'T') {
        return {%$health, state => 'unknown', error => 'Grass process changed during the provider check'};
    }
    return $health;
}
sub valid_ipv4 {
    my $value = $_[0];
    return 0 unless defined($value) && !ref($value) && $value =~ /\A(?:\d{1,3}\.){3}\d{1,3}\z/;
    return !grep {$_ > 255 || (length($_) > 1 && /^0/)} split /\./, $value;
}
sub recovery_action {
    my ($health, $saved, $at) = @_;
    my $state = $health->{state};
    if ($state eq 'connected') {
        delete $saved->{bad_since_ms};
        $saved->{healthy_since_ms} //= $at;
        $saved->{attempts} = 0 if $at - $saved->{healthy_since_ms} >= $HEALTHY_RESET_MS;
        return undef;
    }
    delete $saved->{healthy_since_ms};
    if ($state eq 'unknown' || $state eq 'network_rejected' || $state eq 'login_required'
        || ($state eq 'offline' && ($health->{failure_source} // '') ne 'native_disconnect')) {
        delete $saved->{bad_since_ms};
        return undef;
    }
    if (($saved->{bad_state} // '') ne $state) {
        delete $saved->{bad_since_ms};
    }
    $saved->{bad_state} = $state;
    $saved->{bad_since_ms} //= $at;
    return undef if $at < ($saved->{next_action_ms} // 0);
    my $grace = $state eq 'client_stalled' ? 0 : $state eq 'client_missing' ? 20_000 : $OFFLINE_GRACE_MS;
    return undef if $at - $saved->{bad_since_ms} < $grace;
    if (($saved->{attempts} // 0) >= $MAX_RESTARTS) {
        return 'recovery_exhausted';
    }
    return 'restart_app';
}
sub recovery_confirmed {
    my ($health, $saved) = @_;
    return 0 unless $health->{state} eq 'connected' && $health->{process_identity};
    my $previous = $saved->{old_process_identity};
    return 0 if defined($previous) ? $health->{process_identity} eq $previous : !$saved->{before_restart_process_missing};
    my $native = $health->{native} // {};
    return 0 unless ($native->{cached_status} // '') eq 'CONNECTED' && ($native->{established_tcp} // 0) > 0;
    return 0 unless defined($health->{connected_at_ms}) && $health->{connected_at_ms} >= ($saved->{requested_ms} // 0);
    return 1;
}
sub perform_action {
    my ($ctx, $action) = @_;
    my $saved = $ctx->{recovery};
    my $at = now_ms();
    my $reason = $ctx->{health}{state};
    $saved->{last_action} = $action;
    $saved->{last_reason} = $reason;
    $saved->{requested_ms} = $at;
    my $previous = $ctx->{native_previous} // {};
    $saved->{old_process_identity} = $ctx->{health}{process_identity} // $previous->{process_identity};
    $saved->{old_pid} = $ctx->{health}{pid};
    $saved->{old_pid} = 0 + $1 if !defined($saved->{old_pid}) && ($saved->{old_process_identity} // '') =~ /\A(\d+):/;
    $saved->{predecessor_source} = defined($ctx->{health}{process_identity}) ? 'current_native_process'
        : defined($previous->{process_identity}) ? 'last_observed_native_process' : 'observed_absent';
    $saved->{before_restart_process_missing} = $ctx->{health}{state} eq 'client_missing' ? JSON::PP::true : JSON::PP::false;
    $saved->{next_action_ms} = $at + $RESTART_GRACE_MS;
    delete $saved->{resolved_ms};
    save_recovery($ctx);
    if ($action eq 'recovery_exhausted') {
        $saved->{phase} = 'recovery_exhausted';
        $saved->{error} = 'Local restart budget exhausted; allocation and device identity are retained';
        emit($ctx, 'grass_event', {event => 'replacement_unavailable', action => $action, reason => $reason});
        return;
    }
    emit($ctx, 'grass_event', {event => 'requested', action => $action, reason => $reason, attempt => ($saved->{attempts} // 0) + 1});
    if ($action eq 'restart_app') {
        my ($pid, $state, $ticks) = process();
        kill 'CONT', $pid if $reason eq 'client_stalled' && defined($ticks)
            && "$pid:$ticks" eq ($saved->{old_process_identity} // '');
        if (sysopen(my $pipe, '/run/wdi/restart.fifo', O_WRONLY | O_NONBLOCK)) {
            my $command = 'restart:app:' . ($ctx->{health}{process_identity} // 'missing') . "\n";
            my $written = syswrite($pipe,$command);
            my $ok = defined($written) && $written == length($command);
            close $pipe;
            if ($ok) {
                $saved->{attempts} = ($saved->{attempts} // 0) + 1;
                $saved->{phase} = 'restarting';
                delete $saved->{error};
                emit($ctx, 'grass_event', {event => 'accepted', action => $action, reason => $reason});
            } else {
                $saved->{error} = 'Grass supervisor restart request could not be written';
            }
        } else {
            $saved->{error} = 'Grass supervisor command pipe is unavailable';
        }
    }
    emit($ctx, 'grass_event', {event => 'failed', action => $action, reason => $reason, error => $saved->{error}}) if $saved->{error};
    save_recovery($ctx);
}
sub sample {
    my ($ctx, $final) = @_;
    my $container_rx = read_file('/sys/class/net/eth0/statistics/rx_bytes');
    my $container_tx = read_file('/sys/class/net/eth0/statistics/tx_bytes');
    my $uptime = read_file('/proc/uptime');
    die "Container clock unavailable\n" unless defined($uptime);
    my ($wall, $boot) = (now_ms(), int((split /\s+/, $uptime)[0] * 1000));
    my $meter = eval { decode_json(read_file('/config/run/grass-traffic.json') // '') };
    my $valid = ref($meter) eq 'HASH' && ref($meter->{scope}) eq 'HASH'
        && JSON::PP::is_bool($meter->{valid}) && $meter->{valid}
        && ($meter->{traffic_scope} // '') eq 'application'
        && ($meter->{application} // '') eq 'grass'
        && ($meter->{measurement} // '') eq 'socket_io_payload_bytes'
        && ($meter->{counter_id} // '') =~ /\A[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\z/
        && ($meter->{scope}{group} // '') eq $ctx->{group_id}
        && ($meter->{scope}{instance} // '') eq $ctx->{instance}
        && ($meter->{scope}{machine} // '') eq $ctx->{machine}
        && ($meter->{scope}{runtime} // '') eq $ctx->{runtime};
    for my $name (qw(rx tx source_ms monotonic_ms)) {
        $valid = 0 unless ref($meter) eq 'HASH' && defined($meter->{$name})
            && !ref($meter->{$name}) && $meter->{$name} =~ /\A\d+\z/;
    }
    $valid &&= abs($wall-$meter->{source_ms}) <= 5000 && abs($boot-$meter->{monotonic_ms}) <= 5000
        && valid_ipv4($meter->{ip}) && !$final;
    my $ip = $valid ? $meter->{ip} : undef;
    emit($ctx, 'grass_sample', {seq => $ctx->{seq}++, source_ms => $valid ? $meter->{source_ms} : $wall,
        monotonic_ms => $valid ? $meter->{monotonic_ms} : $boot,
        traffic_scope => 'application', application => 'grass', measurement => 'socket_io_payload_bytes',
        counter_id => $valid ? $meter->{counter_id} : undef,
        valid => $valid ? JSON::PP::true : JSON::PP::false,
        measurement_error => $valid ? undef : 'Grass application counter unavailable, stale or outside this runtime',
        rx => $valid ? $meter->{rx} : undef, tx => $valid ? $meter->{tx} : undef,
        ip => $ip, country => defined($ip) && $ip eq ($ctx->{ip} // '') ? $ctx->{country} : undef,
        container_rx => defined($container_rx) ? 0+$container_rx : undef,
        container_tx => defined($container_tx) ? 0+$container_tx : undef,
        container_counter_source_ms => $wall,
        health => $ctx->{health}, provider => $ctx->{provider}, recovery => $ctx->{recovery},
        final => $final ? JSON::PP::true : JSON::PP::false});
}
sub main {
    umask 0077;
    open my $lock, '>>', '/config/run/grass-worker.lock' or die "Grass worker lock unavailable\n";
    flock($lock, LOCK_EX | LOCK_NB) or exit 0;
    my $instance = $ENV{SALAD_INSTANCE_ID} // die "Salad instance identity unavailable\n";
    my $machine = $ENV{SALAD_MACHINE_ID} // die "Salad machine identity unavailable\n";
    my $group = $ENV{SALAD_CONTAINER_GROUP_ID} // die "Salad group identity unavailable\n";
    my $stat = read_file('/proc/1/stat') // die "Container identity unavailable\n";
    my @stat = split /\s+/, (split /\) /, $stat, 2)[1];
    my $container_run = "$instance:$machine:$stat[19]";
    my $boot_id = read_file('/proc/sys/kernel/random/boot_id') // die "Container boot identity unavailable\n";
    chomp $boot_id;
    my $saved = eval { decode_json(read_file($STATE) // '') } // {};
    $saved = {} unless ($saved->{container_run} // '') eq $container_run;
    delete $saved->{container_run};
    if (($saved->{policy} // '') ne $POLICY) {
        # Prior offline decisions came only from listing absence. Preserve the
        # historical request, but do not spend its restart budget again.
        if (($saved->{last_reason} // '') eq 'offline') {
            $saved->{superseded_request_ms} = $saved->{requested_ms};
            delete @{$saved}{qw(bad_since_ms bad_state next_action_ms healthy_since_ms error)};
            $saved->{attempts} = 0;
            $saved->{phase} = 'unconfirmed_legacy_listing';
        }
        $saved->{policy} = $POLICY;
    }
    # A bounded private pipe isolates recovery decisions from a blocked log sink.
    pipe my $log_read, my $output or die "Container log pipe unavailable\n";
    my $writer = fork();
    die "Container log writer unavailable\n" unless defined $writer;
    if (!$writer) {
        close $output;
        STDOUT->autoflush(1);
        while (my $line = <$log_read>) { print STDOUT $line; }
        exit 0;
    }
    close $log_read;
    fcntl($output,F_SETFL,fcntl($output,F_GETFL,0) | O_NONBLOCK) or die "Container log pipe mode unavailable\n";
    $SIG{PIPE} = 'IGNORE';
    $SIG{CHLD} = 'IGNORE';
    my $ctx = {instance => $instance, machine => $machine, group_id => $group,
        container_run => $container_run, run => $container_run . ':' . now_ms(), output => $output,
        runtime => "$boot_id:$stat[19]",
        seq => 0, recovery => $saved, health => {state => 'starting', observed_ms => now_ms()}};
    my $quit = 0;
    $SIG{TERM} = sub { $quit = 1 };
    $SIG{INT} = sub { $quit = 1 };
    my ($next_health, $next_sample) = (0, 0);
    emit($ctx, 'grass_event', {event => 'worker_started', policy => $POLICY});
    while (!$quit) {
        my $at = now_ms();
        if ($at >= $next_sample) {
            my ($pid, $state, $ticks) = process();
            my $identity = defined($ticks) ? "$pid:$ticks" : '';
            $next_health = 0 if !$pid || ($state // '') eq 'T'
                || $identity ne ($ctx->{health}{process_identity} // '');
        }
        if ($at >= $next_health) {
            $ctx->{health} = observe($ctx);
            my $health = $ctx->{health};
            $next_health = now_ms() + $HEALTH_MS;
            $health->{next_check_ms} = $next_health;
            if (($saved->{phase} // '') =~ /\A(?:restarting|restart_unconfirmed)\z/ && recovery_confirmed($health, $saved)) {
                $saved->{phase} = 'recovered';
                $saved->{resolved_ms} = now_ms();
                delete $saved->{error};
                emit($ctx, 'grass_event', {event => 'recovered', action => 'restart_app', reason => $saved->{last_reason}, requested_ms => $saved->{requested_ms}});
            } elsif (($saved->{phase} // '') eq 'restarting' && now_ms() >= ($saved->{next_action_ms} // 0)) {
                $saved->{phase} = 'restart_unconfirmed';
                $saved->{error} = 'The requested app restart has no confirmed replacement process and connection yet';
                emit($ctx, 'grass_event', {event => 'unconfirmed', action => 'restart_app', requested_ms => $saved->{requested_ms}});
            }
            my $action = recovery_action($health, $saved, now_ms());
            perform_action($ctx, $action) if defined $action;
            save_recovery($ctx);
        }
        if (now_ms() >= $next_sample) {
            sample($ctx, 0);
            $next_sample = (int(now_ms() / $SAMPLE_MS) + 1) * $SAMPLE_MS;
        }
        sleep 1;
    }
    $ctx->{health} = {state => 'stopping', observed_ms => now_ms()};
    sample($ctx, 1);
    emit($ctx, 'grass_event', {event => 'worker_stopped'});
}

main() unless caller;
