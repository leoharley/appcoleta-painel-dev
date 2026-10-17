<?php
defined('BASEPATH') OR exit('No direct script access allowed');

/**
 * ==========================================================================
 * Configuração de CORS para a API do AppColeta
 * ==========================================================================
 *
 * Whitelist de origens (Origin header) autorizadas a chamar /api/*.
 *
 * NÃO usar '*' porque a API exige Authorization: Bearer. Segundo o CORS,
 * quando Access-Control-Allow-Credentials NÃO é 'true' o browser aceita
 * `*` para GET simples, mas para preflight com Authorization ele exige
 * o Origin específico devolvido.
 *
 * Como funciona: se o Origin da requisição estiver na whitelist,
 * o helper `cors_setup()` devolve exatamente esse Origin no header
 * Access-Control-Allow-Origin. Origens fora da lista NÃO recebem o header
 * (o browser bloqueia a chamada — comportamento seguro por padrão).
 *
 * Para o PWA em produção, adicione o domínio definitivo aqui e faça
 * o upload deste arquivo.
 */
$config['cors_allowed_origins'] = array(
    // Desenvolvimento — flutter run -d chrome --web-port 5000
    'http://localhost:5000',
    'http://127.0.0.1:5000',

    // Outras portas comuns em dev (Flutter às vezes usa random)
    'http://localhost:8080',
    'http://127.0.0.1:8080',

    // Painel administrativo (mesma origem já não sofre CORS, mas mantém explícito)
    'https://institutoalveslima.com.br',
    'http://institutoalveslima.com.br',

    // Ambiente de evoluções (VPS): PWA em subdomínio separado do painel/API
    'https://app.institutoalveslima.online',

    // TODO: quando o PWA tiver domínio definitivo, adicione aqui.
    // Exemplo:
    // 'https://coleta.institutoalveslima.com.br',
);

/**
 * Métodos HTTP autorizados. Deixamos todos os comuns porque o app usa
 * GET (listagens), POST (submissões), OPTIONS (preflight).
 */
$config['cors_allowed_methods'] = 'GET, POST, PUT, DELETE, OPTIONS';

/**
 * Cabeçalhos que o cliente pode enviar. Authorization é obrigatório
 * para o Bearer token; Content-Type para JSON e multipart.
 */
$config['cors_allowed_headers'] = 'Authorization, Content-Type, X-Requested-With, Accept';

/**
 * Tempo (em segundos) que o browser pode cachear a resposta do preflight,
 * reduzindo o número de OPTIONS. 24h é um valor conservador.
 */
$config['cors_max_age'] = 86400;
