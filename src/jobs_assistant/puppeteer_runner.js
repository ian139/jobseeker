#!/usr/bin/env node
'use strict';

let puppeteer;
try {
  puppeteer = require(require.resolve('puppeteer', { paths: [process.cwd(), __dirname] }));
} catch (error) {
  process.stdout.write(JSON.stringify({ ok: false, error: `Puppeteer is not installed; run npm install in the runtime working directory: ${error.message}` }) + '\n');
  process.exit(0);
}

const readline = require('node:readline');
const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });

let browser = null;
let page = null;
let commandQueue = Promise.resolve();

function send(data) {
  process.stdout.write(JSON.stringify({ ok: true, data }) + '\n');
}

function fail(error) {
  process.stdout.write(JSON.stringify({ ok: false, error: error && error.message ? error.message : String(error) }) + '\n');
}

async function element(selector) {
  await page.waitForSelector(selector, { timeout: 3000 });
  return selector;
}

async function handle(command) {
  switch (command.action) {
    case 'launch': {
      const launchOptions = { headless: command.headless !== false };
      if (process.env.PUPPETEER_NO_SANDBOX === '1') {
        launchOptions.args = ['--no-sandbox', '--disable-setuid-sandbox'];
      }
      browser = await puppeteer.launch(launchOptions);
      page = await browser.newPage();
      send({});
      return;
    }
    case 'goto': {
      if (!page) throw new Error('Puppeteer page is not initialized');
      await page.goto(command.url, { waitUntil: 'domcontentloaded', timeout: 30000 });
      send({ url: page.url(), title: await page.title(), html: await page.content() });
      return;
    }
    case 'fill': {
      const selector = await element(command.selector);
      await page.focus(selector);
      await page.$eval(selector, el => {
        el.value = '';
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
      });
      await page.type(selector, String(command.value || ''), { delay: 0 });
      send({});
      return;
    }
    case 'select': {
      await element(command.selector);
      await page.select(command.selector, String(command.value || ''));
      send({});
      return;
    }
    case 'upload': {
      await element(command.selector);
      const input = await page.$(command.selector);
      if (!input) throw new Error(`Upload field not found: ${command.selector}`);
      await input.uploadFile(String(command.value || ''));
      send({});
      return;
    }
    case 'click': {
      await element(command.selector);
      await page.click(command.selector, { delay: 0 });
      send({});
      return;
    }
    case 'screenshot': {
      const base64 = await page.screenshot({ fullPage: true, encoding: 'base64' });
      send({ base64 });
      return;
    }
    case 'close': {
      if (browser) await browser.close();
      browser = null;
      page = null;
      send({});
      process.exit(0);
      return;
    }
    default:
      throw new Error(`Unknown Puppeteer action: ${command.action}`);
  }
}

process.stdout.write(JSON.stringify({ ok: true, data: { ready: true } }) + '\n');

rl.on('line', line => {
  commandQueue = commandQueue.then(async () => {
    try {
      await handle(JSON.parse(line));
    } catch (error) {
      fail(error);
    }
  });
});

rl.on('close', () => {
  commandQueue
    .then(async () => {
      if (browser) await browser.close();
    })
    .finally(() => process.exit(0));
});

process.on('SIGTERM', async () => {
  try {
    if (browser) await browser.close();
  } finally {
    process.exit(0);
  }
});
