#pragma once
#include "batch_ota.h"
#include <QGroupBox>
#include <QJsonObject>
#include <QSet>
#include <QTimer>
class MainWindow;
class SecurityPanel;
class MlinkSession;
class QSpinBox;
class QTableWidget;
class QPushButton;
class QLabel;
class QCheckBox;

class RttBatchOtaPanel final : public QGroupBox {
    Q_OBJECT
public:
    RttBatchOtaPanel(MainWindow &, SecurityPanel &, MlinkSession &, QWidget *parent = nullptr);
    ~RttBatchOtaPanel() override;
    bool active() const { return m_controller.active(); }
    QJsonObject state() const;
public slots:
    void startChecked();
    void retryFailed();
    void continueBinding();
    void cancelAll();
private:
    void syncChecks();
    void applyRssiFilter();
    bool visibleDevice(int index) const;
    void checkAll(bool);
    void begin(QVector<RttOtaDevice>);
    void setActive(bool);
    void refresh();
    void exportResults();
    QVector<int> selectedRows() const;
    MainWindow &m_window;
    SecurityPanel &m_security;
    MlinkSession &m_session;
    RttBatchOtaController m_controller;
    QSet<QString> m_checked;
    QSpinBox *m_concurrency;
    QCheckBox *m_throughput;
    QCheckBox *m_fitAtt;
    QCheckBox *m_eventDrive;
    QCheckBox *m_rssiEnabled;
    QSpinBox *m_rssiMinimum;
    QLabel *m_rssiSummary;
    QTableWidget *m_table;
    QLabel *m_summary, *m_notice;
    QPushButton *m_start, *m_all, *m_none, *m_cancel, *m_cancelSelected, *m_retry, *m_bind, *m_export;
    QTimer m_tick;
    bool m_syncing = false;
    qulonglong m_refreshCount = 0;
};
