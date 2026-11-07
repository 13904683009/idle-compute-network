// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title NodeRegistry (Phase 1 prototype)
/// @notice Minimal, non-payable node registry for discovery/capability claims.
///         No tokens, no transfers, no escrow. Not audited; testnet/local only.
contract NodeRegistry {
    struct Node {
        address owner;
        uint8 role;              // 1 = worker, 2 = client
        bytes32 tlsFingerprint;  // optional for outbound-only nodes
        bytes32 capabilityHash;  // optional for clients
        bytes32 endpointHash;    // hash of host:port, optional
        bool active;
    }

    mapping(bytes32 => Node) private _nodes;
    bytes32[] private _activeKeys;
    mapping(bytes32 => uint256) private _activeIndex; // 1-based

    event NodeRegistered(
        bytes32 indexed nodeKey,
        address indexed owner,
        uint8 role,
        bytes32 tlsFingerprint,
        bytes32 capabilityHash,
        bytes32 endpointHash
    );
    event NodeDeactivated(bytes32 indexed nodeKey);

    error AlreadyRegistered();
    error NotOwner();
    error UnknownNode();
    error InvalidNodeKey();
    error InvalidRole();

    function register(
        bytes32 nodeKey,
        uint8 role,
        bytes32 tlsFingerprint,
        bytes32 capabilityHash,
        bytes32 endpointHash
    ) external {
        if (nodeKey == bytes32(0)) revert InvalidNodeKey();
        if (role != 1 && role != 2) revert InvalidRole();
        if (_nodes[nodeKey].active) revert AlreadyRegistered();
        _nodes[nodeKey] = Node({
            owner: msg.sender,
            role: role,
            tlsFingerprint: tlsFingerprint,
            capabilityHash: capabilityHash,
            endpointHash: endpointHash,
            active: true
        });
        _activeKeys.push(nodeKey);
        _activeIndex[nodeKey] = _activeKeys.length;
        emit NodeRegistered(nodeKey, msg.sender, role, tlsFingerprint, capabilityHash, endpointHash);
    }

    function deactivate(bytes32 nodeKey) external {
        Node storage node = _nodes[nodeKey];
        if (!node.active) revert UnknownNode();
        if (node.owner != msg.sender) revert NotOwner();
        node.active = false;
        uint256 idx = _activeIndex[nodeKey];
        if (idx != 0) {
            uint256 last = _activeKeys.length;
            if (idx != last) {
                bytes32 moved = _activeKeys[last - 1];
                _activeKeys[idx - 1] = moved;
                _activeIndex[moved] = idx;
            }
            _activeKeys.pop();
            delete _activeIndex[nodeKey];
        }
        emit NodeDeactivated(nodeKey);
    }

    function active(bytes32 nodeKey) external view returns (bool) {
        return _nodes[nodeKey].active;
    }

    function get(bytes32 nodeKey)
        external
        view
        returns (
            address owner,
            uint8 role,
            bytes32 tlsFingerprint,
            bytes32 capabilityHash,
            bytes32 endpointHash,
            bool isActive
        )
    {
        Node storage node = _nodes[nodeKey];
        return (
            node.owner,
            node.role,
            node.tlsFingerprint,
            node.capabilityHash,
            node.endpointHash,
            node.active
        );
    }

    function activeCount() external view returns (uint256) {
        return _activeKeys.length;
    }

    function activeAt(uint256 index) external view returns (bytes32) {
        return _activeKeys[index];
    }

    function listActive() external view returns (bytes32[] memory) {
        return _activeKeys;
    }
}
