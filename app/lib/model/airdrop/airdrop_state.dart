/// Events from the Linux AirDrop helper are intentionally separate from the
/// LocalSend protocol and its device/session models.
class AirDropPeer {
  final String id;
  final String name;

  const AirDropPeer({required this.id, required this.name});

  factory AirDropPeer.fromJson(Map<String, dynamic> json) {
    return AirDropPeer(id: json['id'] as String, name: json['name'] as String? ?? 'Apple device');
  }
}

class AirDropOffer {
  final String id;
  final String sender;
  final List<String> files;

  const AirDropOffer({required this.id, required this.sender, required this.files});

  factory AirDropOffer.fromJson(Map<String, dynamic> json) {
    return AirDropOffer(
      id: json['offerId'] as String,
      sender: json['sender'] as String? ?? 'Apple device',
      files: (json['files'] as List).map((file) => (file as Map)['name'] as String).toList(),
    );
  }
}

class AirDropState {
  final bool available;
  final String status;
  final String detail;
  final List<AirDropPeer> peers;
  final List<AirDropOffer> offers;
  final List<String> transfers;

  const AirDropState({
    this.available = false,
    this.status = 'unchecked',
    this.detail = '',
    this.peers = const [],
    this.offers = const [],
    this.transfers = const [],
  });

  bool get running => status == 'discovering';

  AirDropState copyWith({
    bool? available,
    String? status,
    String? detail,
    List<AirDropPeer>? peers,
    List<AirDropOffer>? offers,
    List<String>? transfers,
  }) {
    return AirDropState(
      available: available ?? this.available,
      status: status ?? this.status,
      detail: detail ?? this.detail,
      peers: peers ?? this.peers,
      offers: offers ?? this.offers,
      transfers: transfers ?? this.transfers,
    );
  }

  AirDropState apply(Map<String, dynamic> event) {
    switch (event['type']) {
      case 'status':
        final next = event['state'] as String;
        return copyWith(
          status: next,
          detail: event['detail'] as String? ?? '',
          peers: next == 'discovering' ? null : [],
          offers: next == 'discovering' ? null : [],
        );
      case 'peers':
        return copyWith(peers: (event['peers'] as List).map((peer) => AirDropPeer.fromJson(Map<String, dynamic>.from(peer as Map))).toList());
      case 'offer':
        final offer = AirDropOffer.fromJson(event);
        return copyWith(offers: [...offers.where((existing) => existing.id != offer.id), offer]);
      case 'transfer':
        final state = event['state'] as String;
        final offerId = event['offerId'];
        final terminal = ['rejected', 'expired', 'completed', 'failed', 'error', 'cancelled'].contains(state);
        final description = '${event['direction'] == 'receive' ? 'Receive' : 'Send'}: ${event['detail'] ?? state}';
        return copyWith(
          offers: terminal ? offers.where((offer) => offer.id != offerId).toList() : null,
          transfers: [description, ...transfers].take(10).toList(),
        );
      default:
        return this;
    }
  }
}
