import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:localsend_app/model/airdrop/airdrop_state.dart';
import 'package:localsend_app/util/airdrop/airdrop_service.dart';

class FakeConnection implements AirDropConnection {
  final output = StreamController<String>.broadcast(sync: true);
  final diagnostics = StreamController<String>.broadcast(sync: true);
  final exited = Completer<int>();
  final commands = <Map<String, dynamic>>[];
  bool closed = false;
  bool reply = true;

  @override
  Stream<String> get lines => output.stream;
  @override
  Stream<String> get errors => diagnostics.stream;
  @override
  Future<int> get exitCode => exited.future;

  void event(Map<String, dynamic> event) => output.add(jsonEncode(event));

  @override
  void write(String line) {
    final request = jsonDecode(line) as Map<String, dynamic>;
    commands.add(request);
    if (reply) {
      event({
        'type': 'response',
        'id': request['id'],
        'ok': true,
        'result': request['command'] == 'probe' ? {'available': true, 'detail': 'OWL ready'} : {},
      });
    }
  }

  @override
  void close() => closed = true;

  Future<void> dispose() async {
    await output.close();
    await diagnostics.close();
    if (!exited.isCompleted) exited.complete(0);
  }
}

void main() {
  test('unsupported hosts never launch the Linux helper', () async {
    var launches = 0;
    final service = AirDropService(
      supported: false,
      onChanged: (_) {},
      connect: () async {
        launches++;
        return FakeConnection();
      },
    );
    await service.probe();
    expect(service.state.status, 'unsupported');
    expect(service.state.available, false);
    expect(launches, 0);
    await expectLater(service.command('start'), throwsUnsupportedError);
    service.dispose();
  });

  test('probe, discovery, send and acceptance use correlated JSONL commands', () async {
    final connection = FakeConnection();
    final service = AirDropService(supported: true, connect: () async => connection, onChanged: (_) {});
    addTearDown(() async {
      service.dispose();
      await connection.dispose();
    });
    await service.probe();
    expect(service.state.available, true);
    expect(service.state.detail, 'OWL ready');
    final folder = Directory.systemTemp.path;
    await service.start(name: 'My LocalDrop', downloadDir: folder);
    expect(connection.commands.last, containsPair('downloadDir', folder));
    expect(connection.commands.last, containsPair('seconds', 300));
    connection.event({'type': 'status', 'state': 'discovering', 'detail': 'Ready'});
    connection.event({
      'type': 'peers',
      'peers': [
        {'id': 'peer-1', 'name': 'Mac'},
      ],
    });
    expect(service.state.running, true);
    expect(service.state.peers.single.name, 'Mac');
    final path = '${Directory.systemTemp.path}${Platform.pathSeparator}file with spaces.txt';
    await service.send('peer-1', [path]);
    expect(connection.commands.last['paths'], [path]);
    expect(connection.commands.last['peerId'], 'peer-1');
    connection.event({
      'type': 'offer',
      'offerId': 'offer-1',
      'sender': 'Mac',
      'files': [
        {'name': 'hello.txt', 'size': 7},
      ],
    });
    expect(service.state.offers.single.files, ['hello.txt']);
    await service.decide('offer-1', false);
    expect(connection.commands.last['accept'], false);
    expect(service.state.offers, isEmpty);
    connection.event({
      'type': 'offer',
      'offerId': 'offer-2',
      'sender': 'Mac',
      'files': [
        {'name': 'accepted.txt'},
      ],
    });
    await service.decide('offer-2', true);
    expect(connection.commands.last['accept'], true);
    expect(service.state.offers, isEmpty);
    await service.stop();
    connection.event({'type': 'status', 'state': 'stopped'});
    expect(service.state.peers, isEmpty);
    expect(service.state.running, false);
    expect(connection.commands.map((command) => command['id']).toSet().length, connection.commands.length);
  });

  test('malformed helper output does not prevent later valid events', () async {
    final connection = FakeConnection();
    final service = AirDropService(supported: true, connect: () async => connection, onChanged: (_) {});
    await service.probe();
    connection.output.add('not json');
    expect(service.state.detail, contains('invalid response'));
    connection.event({
      'type': 'peers',
      'peers': [
        {'id': 'p', 'name': 'iPhone'},
      ],
    });
    expect(service.state.peers.single.name, 'iPhone');
    service.dispose();
    expect(connection.closed, true);
    await connection.dispose();
  });

  test('concurrent requests correlate responses regardless of response order', () async {
    final connection = FakeConnection()..reply = false;
    final service = AirDropService(supported: true, connect: () async => connection, onChanged: (_) {});
    final first = service.command('peers');
    final second = service.command('probe');
    await Future<void>.delayed(Duration.zero);
    expect(connection.commands.length, 2);
    connection.event({
      'type': 'response',
      'id': connection.commands[1]['id'],
      'ok': true,
      'result': {'second': true},
    });
    connection.event({
      'type': 'response',
      'id': connection.commands[0]['id'],
      'ok': true,
      'result': {'first': true},
    });
    expect(await first, {'first': true});
    expect(await second, {'second': true});
    service.dispose();
    await connection.dispose();
  });

  test('a missing helper reports installation instructions without throwing', () async {
    final service = AirDropService(
      supported: true,
      connect: () async => throw const ProcessException('localdrop-airdrop', [], 'missing'),
      onChanged: (_) {},
    );
    await service.probe();
    expect(service.state.available, false);
    expect(service.state.detail, contains('Install the LocalDrop Linux AirDrop helper'));
    service.dispose();
  });

  test('helper exit fails outstanding requests and clears stale devices', () async {
    final connection = FakeConnection();
    final service = AirDropService(supported: true, connect: () async => connection, onChanged: (_) {});
    await service.probe();
    connection.event({
      'type': 'peers',
      'peers': [
        {'id': 'p', 'name': 'Mac'},
      ],
    });
    connection.reply = false;
    final request = service.command('peers');
    final expectation = expectLater(request, throwsStateError);
    await Future<void>.delayed(Duration.zero);
    connection.exited.complete(1);
    await expectation;
    expect(service.state.available, false);
    expect(service.state.peers, isEmpty);
    service.dispose();
    await connection.dispose();
  });

  test('timed out requests discard late responses and permit later requests', () async {
    final connection = FakeConnection()..reply = false;
    final service = AirDropService(supported: true, connect: () async => connection, timeout: const Duration(milliseconds: 10), onChanged: (_) {});
    await expectLater(service.command('peers'), throwsA(isA<TimeoutException>()));
    connection.event({'type': 'response', 'id': connection.commands.last['id'], 'ok': true});
    connection.reply = true;
    await service.probe();
    expect(service.state.available, true);
    await expectLater(service.send('p', ['relative.txt']), throwsArgumentError);
    service.dispose();
    await connection.dispose();
  });

  test('terminal transfer events expire offers and bound the activity history', () {
    var state = const AirDropState();
    state = state.apply({
      'type': 'offer',
      'offerId': 'offer',
      'sender': 'Mac',
      'files': [
        {'name': 'a.txt'},
      ],
    });
    state = state.apply({'type': 'transfer', 'direction': 'receive', 'state': 'expired', 'offerId': 'offer'});
    expect(state.offers, isEmpty);
    for (var index = 0; index < 20; index++) {
      state = state.apply({'type': 'transfer', 'direction': 'send', 'state': 'completed', 'detail': '$index'});
    }
    expect(state.transfers.length, 10);
    expect(state.transfers.first, 'Send: 19');
  });
}
