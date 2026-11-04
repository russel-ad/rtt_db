/* Adapter to unmodified SDK libmlink. One context per BLE connection. */
#include "mlink/link.h"
#include "mlink/json.h"
#include <stdlib.h>
#include <string.h>
#define API __declspec(dllexport)
typedef int (*write_fn)(const uint8_t *, uint16_t);
typedef int (*random_fn)(uint8_t *, uint16_t);
typedef void (*receive_fn)(int, const uint8_t *, uint16_t);
typedef void (*done_fn)(uint32_t, int, uint8_t);
typedef struct {
    mlink_link_t link;
    write_fn write;
    random_fn random;
    receive_fn receive;
    done_fn done;
} context_t;
static int write_bytes(void *user, const uint8_t *data, uint16_t n)
{ return ((context_t *)user)->write(data,n); }
static int random_bytes(void *user,uint8_t *data,uint16_t n)
{ return ((context_t *)user)->random(data,n); }
static uint16_t max_write(void *user) { (void)user; return 20; }
static void receive(void *user,const mlink_hdr_t *header,const uint8_t *body,uint16_t n)
{
    cJSON *root=NULL;
    int rc=mlink_flags_kind(header->flags)==MLINK_KIND_JSON ? mlink_json_parse(body,n,&root):MLINK_ERR_ARG;
    cJSON_Delete(root); ((context_t *)user)->receive(rc,body,n);
}
static void done(void *user,uint32_t tag,int result,uint8_t reason)
{ ((context_t *)user)->done(tag,result,reason); }
API void *fp_create(write_fn write,random_fn random,receive_fn rx,done_fn tx_done)
{
    context_t *c=calloc(1,sizeof(*c)); if(!c)return NULL;
    c->write=write; c->random=random; c->receive=rx; c->done=tx_done;
    mlink_link_cfg_t cfg; mlink_link_cfg_init(&cfg);
    cfg.media=MLINK_MEDIA_BLE; cfg.self_board=MLINK_BOARD_MAIN;
    cfg.json=1; cfg.max_body=512;
    cfg.port.write=write_bytes; cfg.port.rand_fill=random_bytes; cfg.port.max_write=max_write;
    cfg.port.user=c; cfg.user=c; cfg.on_rx=receive; cfg.on_done=done;
    if(mlink_link_init(&c->link,&cfg)!=MLINK_OK) { free(c); return NULL; }
    return c;
}
API void fp_destroy(context_t *c) { free(c); }
API void fp_receive(context_t *c,const uint8_t *bytes,uint16_t n,uint32_t now)
{ mlink_link_rx_bytes(&c->link,bytes,n,now); }
API int fp_poll(context_t *c,uint32_t now) { return mlink_link_poll(&c->link,now); }
API int fp_hello(context_t *c) { return mlink_link_start_handshake(&c->link); }
API int fp_up(context_t *c) { return mlink_link_up(&c->link); }
API int fp_send(context_t *c,const uint8_t *bytes,uint16_t n,uint32_t tag)
{
    cJSON *root=NULL; int rc=mlink_json_parse(bytes,n,&root);
    if(rc!=MLINK_OK)return rc;
    const cJSON *cmd=cJSON_GetObjectItemCaseSensitive(root,"cmd");
    int allowed=cJSON_IsString(cmd) && (!strcmp(cmd->valuestring,"discover") ||
        !strcmp(cmd->valuestring,"factory.pair") || !strcmp(cmd->valuestring,"factory.pair.info") ||
        !strcmp(cmd->valuestring,"factory.pair.result"));
    cJSON_Delete(root); if(!allowed || n>512)return MLINK_ERR_ARG;
    mlink_hdr_t h={0}; h.version=MLINK_VER; h.flags=mlink_flags(MLINK_KIND_JSON,MLINK_WANT_ACK,0);
    h.hop=MLINK_HOP_FWD; h.src_board=MLINK_BOARD_MAIN; h.dst_board=MLINK_BOARD_MAIN;
    return mlink_link_send_pdu(&c->link,&h,bytes,n,tag);
}
