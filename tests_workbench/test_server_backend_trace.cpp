// Reuse the original bounded fake HTTP transport and canonical credential
// fixtures. All responses are local; this executable never logs into a server
// or opens a BLE device.
#define main rttSourceServerBackendMain
#include "test_server_backend.cpp"
#undef main

namespace {
QByteArray traced(const QJsonObject &data, const QJsonValue &trace)
{
    auto object = QJsonDocument::fromJson(envelope(data)).object();
    object.insert("requestId", trace);
    return QJsonDocument(object).toJson(QJsonDocument::Compact);
}

void loginTrace()
{
    Network network;
    ServerLogin session(nullptr, &network);
    auto c = config(); c.timeoutMs = 1000;
    const auto token = jwt({{"a", QString::fromLatin1(loginUid().toHex())}});
    const QJsonObject data{{"tokenType", "bearer"}, {"accessToken", QString::fromLatin1(token)},
                           {"expiresIn", 3600}};
    const auto run = [&](const QByteArray &bytes) {
        network.responses.append({bytes});
        return result<ServerLoginSession>([&](auto callback) {
            session.login(c, true, "synthetic-account", "synthetic-password", callback);
        });
    };
    ensure(run(envelope(data)).ok(), "A response without requestId stopped working");
    const QList<QJsonValue> valid{QJsonValue(QJsonValue::Null), "", "login.trace-01",
                                 QString(256, QLatin1Char('a'))};
    for (const auto &trace : valid) {
        const auto reply = run(traced(data, trace));
        ensure(reply.ok() && reply.value.uid == loginUid() &&
               reply.value.authorization == QByteArray("bearer") + token,
               "Optional envelope requestId changed or rejected the login identity");
    }
    const QList<QJsonValue> invalid{true, 42, QJsonArray(), QJsonObject(),
                                   QString(257, QLatin1Char('a'))};
    for (const auto &trace : invalid)
        ensure(run(traced(data, trace)).error == BackendErrorKind::InvalidResponse,
               "Malformed or overlong requestId was accepted");
    auto unknown = QJsonDocument::fromJson(traced(data, "trace")).object();
    unknown.insert("unexpected", true);
    ensure(run(QJsonDocument(unknown).toJson()).error == BackendErrorKind::InvalidResponse,
           "Unknown envelope metadata was accepted");
    auto badData = data;
    badData.insert("rootKey", "forbidden");
    ensure(run(traced(badData, "trace")).error == BackendErrorKind::InvalidResponse,
           "Outer trace allowed a forbidden Root field in login data");
    network.responses.append({traced(data, "trace"), 0, 1, QNetworkReply::ConnectionRefusedError});
    const auto refused = result<ServerLoginSession>([&](auto callback) {
        session.login(c, true, "synthetic-account", "synthetic-password", callback);
    });
    ensure(refused.error == BackendErrorKind::Network && refused.code == 1,
           "Connection refused was confused with JSON schema rejection");
    pass("requestId login: absent/null/string/256 accepted; invalid types/257/unknown/Root rejected; transport errors preserved");
}

void credentialTrace(const QString &directory)
{
    Network network;
    auto c = config(); c.timeoutMs = 1000;
    ServerBackend backend(c, directory, nullptr, &network);
    const auto grant = issue(root(), target, c.uid, 3, MLINK_PERMISSION_APP);
    const auto run = [&](const QJsonObject &data, const QJsonValue &trace) {
        network.responses.append({traced(data, trace)});
        return result<Credential>([&](auto callback) {
            backend.fetchLocal(target, c.account(), callback);
        });
    };
    const auto accepted = run(dto(grant), "credential.trace-01");
    ensure(accepted.ok() && accepted.value.identity == grant.identity &&
           std::memcmp(accepted.value.psk.data(), grant.psk.data(), 32) == 0,
           "Outer requestId changed the validated App credential");
    ensure(run(dto(grant), QJsonObject()).error == BackendErrorKind::InvalidResponse,
           "Malformed credential trace was accepted");
    for (int test = 0; test < 6; ++test) {
        auto bad = dto(grant);
        if (test == 0) bad.insert("uid", QString(32, QLatin1Char('a')));
        if (test == 1) bad.insert("deviceSn", "M21010000A018CE");
        if (test == 2) bad.insert("permissions", 1);
        if (test == 3) bad.insert("tlsIdentity", QString::fromLatin1(grant.identity) + '=');
        if (test == 4) bad.insert("rootKey", "forbidden");
        if (test == 5) bad.insert("requestId", "not-a-credential-field");
        ensure(run(bad, "trace").error == BackendErrorKind::InvalidResponse,
               "Outer requestId weakened credential data validation");
    }
    pass("requestId credentials: valid App accepted; UID/SN/permission/TLS identity/Root/data-field checks retained");
}
}

int main(int argc, char **argv)
{
    QCoreApplication application(argc, argv);
    try {
        QTemporaryDir directory;
        ensure(directory.isValid(), "Cannot isolate backend trace tests");
        codecs();
        transport(directory.path() + "/transport");
        login(directory.path() + "/login");
        claims(directory.path() + "/claims");
        loginTrace();
        credentialTrace(directory.path() + "/trace");
        return 0;
    } catch (const std::exception &error) {
        std::fprintf(stderr, "FAIL %s\n", error.what());
        return 1;
    }
}
