import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:localsend_app/model/airdrop/airdrop_state.dart';

abstract class AirDropConnection {
  Stream<String> get lines;
  Stream<String> get errors;
  Future<int> get exitCode;
  void write(String line);
  void close();
}

class _ProcessConnection implements AirDropConnection {
  final Process process;

  _ProcessConnection(this.process);

  @override
  Stream<String> get lines => process.stdout.transform(utf8.decoder).transform(const LineSplitter());
  @override
  Stream<String> get errors => process.stderr.transform(utf8.decoder).transform(const LineSplitter());
  @override
  Future<int> get exitCode => process.exitCode;
  @override
  void write(String line) => process.stdin.writeln(line);
  @override
  void close() {
    try {
      process.stdin.writeln(jsonEncode({'id': 'shutdown', 'command': 'shutdown'}));
      unawaited(process.stdin.close().catchError((Object _) {}));
    } catch (_) {
      process.kill();
    }
    unawaited(
      process.exitCode.timeout(
        const Duration(seconds: 55),
        onTimeout: () {
          process.kill();
          return -1;
        },
      ),
    );
  }
}

typedef AirDropConnector = Future<AirDropConnection> Function();

/// Owns a long-lived helper process; commands never pass through a shell.
class AirDropService {
  final bool supported;
  final AirDropConnector _connect;
  final Duration timeout;
  final void Function(AirDropState) onChanged;
  AirDropState state = const AirDropState();
  AirDropConnection? _connection;
  Future<void>? _connecting;
  StreamSubscription<String>? _stdout;
  StreamSubscription<String>? _stderr;
  final Map<String, Completer<Map<String, dynamic>>> _pending = {};
  int _nextId = 0;
  bool _disposed = false;

  AirDropService({required this.onChanged, bool? supported, AirDropConnector? connect, this.timeout = const Duration(seconds: 30)})
    : supported = supported ?? Platform.isLinux,
      _connect = connect ?? _startProcess;

  static Future<AirDropConnection> _startProcess() async {
    return _ProcessConnection(await Process.start('/usr/bin/localdrop-airdrop', const [], runInShell: false));
  }

  void _update(AirDropState next) {
    if (_disposed) return;
    state = next;
    onChanged(next);
  }

  void _failPending(String message) {
    for (final request in _pending.values) {
      if (!request.isCompleted) request.completeError(StateError(message));
    }
    _pending.clear();
  }

  Future<void> _ensureConnected() async {
    if (_disposed) throw StateError('AirDrop service has closed.');
    if (_connection != null) return;
    final pendingConnection = _connecting;
    if (pendingConnection != null) return pendingConnection;
    final connecting = _open();
    _connecting = connecting;
    try {
      await connecting;
    } finally {
      _connecting = null;
    }
  }

  Future<void> _open() async {
    final connection = await _connect();
    if (_disposed) {
      connection.close();
      throw StateError('AirDrop service has closed.');
    }
    _connection = connection;
    _stdout = connection.lines.listen(
      _readLine,
      onError: (Object error) => _update(state.copyWith(status: 'error', detail: '$error')),
    );
    // stderr is diagnostic output only, never interpreted as protocol data.
    _stderr = connection.errors.listen((_) {}, onError: (Object _) {});
    unawaited(
      connection.exitCode.then((code) {
        if (_connection != connection || _disposed) return;
        _connection = null;
        _failPending('AirDrop helper exited ($code).');
        _update(
          state.copyWith(
            available: false,
            status: 'error',
            detail: 'AirDrop helper exited ($code). Check the Linux helper installation.',
            peers: [],
            offers: [],
          ),
        );
        unawaited(_stdout?.cancel());
        unawaited(_stderr?.cancel());
      }),
    );
  }

  void _readLine(String line) {
    try {
      final event = Map<String, dynamic>.from(jsonDecode(line) as Map);
      if (event['type'] == 'response') {
        final completer = _pending.remove(event['id']?.toString());
        if (completer == null) return;
        if (event['ok'] == true) {
          final result = event['result'];
          if (result != null && result is! Map) {
            completer.completeError(const FormatException('Invalid AirDrop response result.'));
          } else {
            completer.complete(Map<String, dynamic>.from(result as Map? ?? const {}));
          }
        } else {
          completer.completeError(StateError('${event['error'] ?? 'AirDrop request failed.'}'));
        }
      } else {
        _update(state.apply(event));
      }
    } catch (_) {
      _update(state.copyWith(detail: 'The AirDrop helper sent an invalid response. Check that the app and helper versions match.'));
    }
  }

  Future<Map<String, dynamic>> command(String command, [Map<String, dynamic> arguments = const {}]) async {
    if (!supported) throw UnsupportedError('AirDrop compatibility is currently available on Linux with an AWDL-capable Wi-Fi adapter.');
    await _ensureConnected();
    final id = '${++_nextId}';
    final completer = Completer<Map<String, dynamic>>();
    _pending[id] = completer;
    try {
      _connection!.write(jsonEncode({...arguments, 'id': id, 'command': command}));
      return await completer.future.timeout(switch (command) {
        'start' => const Duration(seconds: 120),
        'stop' => const Duration(seconds: 60),
        _ => timeout,
      });
    } finally {
      _pending.remove(id);
    }
  }

  Future<void> probe() async {
    if (!supported) {
      _update(
        state.copyWith(status: 'unsupported', detail: 'AirDrop compatibility is currently available on Linux with an AWDL-capable Wi-Fi adapter.'),
      );
      return;
    }
    _update(state.copyWith(status: 'checking', detail: 'Checking AirDrop support…'));
    try {
      final result = await command('probe');
      _update(
        state.copyWith(
          available: result['available'] == true,
          status: result['available'] == true ? 'ready' : 'missing_dependencies',
          detail: '${result['detail'] ?? result['reason'] ?? ''}',
        ),
      );
    } catch (error) {
      _update(
        state.copyWith(
          available: false,
          status: 'missing_dependencies',
          detail: 'Install the LocalDrop Linux AirDrop helper, then check support again. $error',
        ),
      );
    }
  }

  Future<void> start({required String name, required String downloadDir}) async {
    await command('start', {'name': name, 'downloadDir': downloadDir, 'seconds': 300});
  }

  Future<void> stop() async {
    await command('stop');
  }

  Future<void> send(String peerId, List<String> paths) async {
    if (paths.isEmpty || paths.any((path) => !File(path).isAbsolute)) throw ArgumentError('Select files with absolute paths.');
    await command('send', {'peerId': peerId, 'paths': paths});
  }

  Future<void> decide(String offerId, bool accept) async {
    await command('decide', {'offerId': offerId, 'accept': accept});
    _update(state.copyWith(offers: state.offers.where((offer) => offer.id != offerId).toList()));
  }

  void dispose() {
    _disposed = true;
    _failPending('AirDrop service has closed.');
    unawaited(_stdout?.cancel());
    unawaited(_stderr?.cancel());
    _connection?.close();
    _connection = null;
  }
}
