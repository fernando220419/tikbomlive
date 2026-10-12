
# ====== Estrutura de dados para o ranking de curtidas ======
# Isso deve ficar junto de onde voce ja guarda os comentarios e presentes,
# provavelmente um dicionario por usuario conectado.

# Exemplo: likers_data[username] = { "nome_de_quem_curtiu": {"name": ..., "avatar": ..., "likes": 0} }
likers_data = {}


# ====== Dentro do handler de evento de curtida da TikTokLive ======
# Procure no seu codigo o trecho tipo:
#   @client.on("like")
#   async def on_like(event):
# e dentro dele adicione a logica de somar curtidas por pessoa:

@client.on("like")
async def on_like(event):
    user_id = str(event.user.unique_id)
    nome = event.user.nickname
    avatar = event.user.avatar_thumb.url_list[0] if event.user.avatar_thumb else ""
    qtd = event.like_count or 1

    if username not in likers_data:
        likers_data[username] = {}

    if user_id not in likers_data[username]:
        likers_data[username][user_id] = {"name": nome, "avatar": avatar, "likes": 0}

    likers_data[username][user_id]["likes"] += qtd


# ====== Novo endpoint para o overlay consultar o ranking ======

@app.route("/api/likers/<username>", methods=["GET"])
def get_likers(username):
    if username not in likers_data:
        return jsonify({"likers": []})

    # ordena do maior para o menor numero de curtidas
    ranking = sorted(
        likers_data[username].values(),
        key=lambda u: u["likes"],
        reverse=True
    )

    # retorna so os 10 primeiros, por exemplo
    return jsonify({"likers": ranking[:10]})
