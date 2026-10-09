import 'dart:async';

import 'package:file_selector/file_selector.dart';
import 'package:flutter/material.dart';
import 'package:localsend_app/model/airdrop/airdrop_state.dart';
import 'package:localsend_app/provider/settings_provider.dart';
import 'package:localsend_app/util/airdrop/airdrop_service.dart';
import 'package:localsend_app/widget/responsive_list_view.dart';
import 'package:path_provider/path_provider.dart';
import 'package:refena_flutter/refena_flutter.dart';

class AirDropTab extends StatefulWidget {
  const AirDropTab();

  @override
  State<AirDropTab> createState() => _AirDropTabState();
}

class _AirDropTabState extends State<AirDropTab> with AutomaticKeepAliveClientMixin, Refena {
  late final AirDropService _service;
  AirDropState _state = const AirDropState();
  List<XFile> _files = [];
  String? _downloadDir;
  String? _error;
  bool _busy = false;
  final Set<String> _decidingOffers = {};

  @override
  bool get wantKeepAlive => true;

  @override
  void initState() {
    super.initState();
    _service = AirDropService(
      onChanged: (state) {
        if (mounted) setState(() => _state = state);
      },
    );
    unawaited(_service.probe());
    unawaited(_loadDownloadDirectory());
  }

  Future<void> _loadDownloadDirectory() async {
    try {
      final directory = await getDownloadsDirectory();
      if (mounted) setState(() => _downloadDir = directory?.path);
    } catch (_) {
      // The user can select a folder when there is no configured Downloads path.
    }
  }

  Future<void> _run(Future<void> Function() action) async {
    if (_busy) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await action();
    } catch (error) {
      if (mounted) setState(() => _error = '$error');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _chooseDirectory() async {
    final directory = await getDirectoryPath(initialDirectory: _downloadDir);
    if (directory != null && mounted) setState(() => _downloadDir = directory);
  }

  Future<void> _chooseFiles() async {
    final files = await openFiles();
    if (files.isNotEmpty && mounted) setState(() => _files = files);
  }

  Future<void> _decideOffer(AirDropOffer offer, bool accept) async {
    if (!_decidingOffers.add(offer.id)) return;
    setState(() => _error = null);
    try {
      await _service.decide(offer.id, accept);
    } catch (error) {
      if (mounted) setState(() => _error = '$error');
    } finally {
      if (mounted) setState(() => _decidingOffers.remove(offer.id));
    }
  }

  @override
  void dispose() {
    _service.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    super.build(context);
    final theme = Theme.of(context);
    return ResponsiveListView(
      padding: const EdgeInsets.all(16),
      children: [
        Text('AirDrop', style: theme.textTheme.headlineSmall),
        const SizedBox(height: 8),
        const Text('Experimental Apple AirDrop compatibility using the Linux omdrop backend. Wi-Fi hardware and backend support are required.'),
        const SizedBox(height: 8),
        const Text('On the Apple device, turn on Wi-Fi and Bluetooth and set AirDrop to Everyone for 10 Minutes.'),
        const SizedBox(height: 16),
        if (_state.status == 'checking') const LinearProgressIndicator(),
        if (_state.detail.isNotEmpty) Padding(padding: const EdgeInsets.only(top: 8, bottom: 8), child: SelectableText(_state.detail)),
        if (_error != null)
          Padding(
            padding: const EdgeInsets.only(bottom: 8),
            child: SelectableText(_error!, style: TextStyle(color: theme.colorScheme.error)),
          ),
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            FilledButton.icon(
              onPressed: _busy || !_state.available || (!_state.running && _downloadDir == null)
                  ? null
                  : () => _run(() async {
                      if (_state.running) {
                        await _service.stop();
                      } else {
                        await _service.start(name: ref.read(settingsProvider).alias, downloadDir: _downloadDir!);
                      }
                    }),
              icon: Icon(_state.running ? Icons.stop : Icons.wifi_find),
              label: Text(_state.running ? 'Stop AirDrop' : 'Start AirDrop for 5 minutes'),
            ),
            TextButton(
              onPressed: _busy || _state.running || _state.status == 'checking' ? null : () => _run(_service.probe),
              child: const Text('Check support'),
            ),
          ],
        ),
        const SizedBox(height: 16),
        Text('Save received files to', style: theme.textTheme.titleMedium),
        const SizedBox(height: 4),
        SelectableText(_downloadDir ?? 'Choose a folder before starting AirDrop.'),
        Align(
          alignment: AlignmentDirectional.centerStart,
          child: TextButton.icon(
            onPressed: _busy || _state.running ? null : () => _run(_chooseDirectory),
            icon: const Icon(Icons.folder_open),
            label: const Text('Choose folder'),
          ),
        ),
        if (_state.offers.isNotEmpty) ...[
          const Divider(),
          Text('Incoming files', style: theme.textTheme.titleMedium),
          for (final offer in _state.offers)
            ListTile(
              contentPadding: EdgeInsets.zero,
              leading: const Icon(Icons.file_download),
              title: Text(offer.sender),
              subtitle: Text(offer.files.join('\n')),
              trailing: Wrap(
                spacing: 8,
                children: [
                  TextButton(onPressed: _decidingOffers.contains(offer.id) ? null : () => _decideOffer(offer, false), child: const Text('Reject')),
                  FilledButton(onPressed: _decidingOffers.contains(offer.id) ? null : () => _decideOffer(offer, true), child: const Text('Accept')),
                ],
              ),
            ),
        ],
        const Divider(),
        Row(
          children: [
            Expanded(child: Text('Files to send', style: theme.textTheme.titleMedium)),
            TextButton.icon(onPressed: _busy ? null : () => _run(_chooseFiles), icon: const Icon(Icons.add), label: const Text('Choose files')),
            if (_files.isNotEmpty)
              IconButton(onPressed: _busy ? null : () => setState(() => _files = []), icon: const Icon(Icons.close), tooltip: 'Clear files'),
          ],
        ),
        if (_files.isEmpty) const Text('Select files, then send them to a nearby Apple device.'),
        for (final file in _files) Text(file.name),
        const SizedBox(height: 16),
        Text('Nearby Apple devices', style: theme.textTheme.titleMedium),
        const SizedBox(height: 8),
        if (_state.peers.isEmpty) Text(_state.running ? 'Looking for nearby devices…' : 'Start AirDrop to discover devices and receive requests.'),
        for (final peer in _state.peers)
          ListTile(
            contentPadding: EdgeInsets.zero,
            leading: const Icon(Icons.devices),
            title: Text(peer.name),
            trailing: FilledButton(
              onPressed: _busy || !_state.running || _files.isEmpty
                  ? null
                  : () => _run(() => _service.send(peer.id, _files.map((file) => file.path).toList())),
              child: const Text('Send'),
            ),
          ),
        if (_state.transfers.isNotEmpty) ...[
          const Divider(),
          Text('Transfers', style: theme.textTheme.titleMedium),
          for (final transfer in _state.transfers) Padding(padding: const EdgeInsets.symmetric(vertical: 4), child: Text(transfer)),
        ],
      ],
    );
  }
}
